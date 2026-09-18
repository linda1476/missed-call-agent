"""Tool-call dispatch shared by the Voice Agent WebSocket client and the
HTTP tool server. Schemas live in schemas/ — the same files the agent's
session.update sends.

Call lifecycle through tools:

  memory_load(caller_id)   -> registers a WorkingMemory for the call and
                              returns its call_id plus the caller's
                              current-value slots + procedural rules
  memory_search(...)       -> append-only history lookup (logged to the call)
  booking_reserve(...)     -> CAS reservation; outcome recorded on the call
  report_emit(call_id)     -> closes the call: runs the fixed post-call
                              handoff (summary->history, facts->current,
                              state record, owner report) and drops the
                              working memory
"""

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..booking.slots import SlotTable
from ..memory.handoff import run_handoff
from ..memory.store import MemoryStore
from ..memory.working import WorkingMemory
from ..report.emit import build_report, validate_report

_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"
_CALL_TTL_S = 3600  # drop working memories older than an hour


@dataclass
class ToolContext:
    store: MemoryStore
    slots: SlotTable
    store_id: str
    active_calls: dict = None  # call_id -> WorkingMemory (in-flight)

    def __post_init__(self):
        if self.active_calls is None:
            self.active_calls = {}


def load_schemas() -> dict[str, dict]:
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_SCHEMAS.glob("*.json"))
        if p.stem != "report_output"
    }


def _req(args: dict, key: str):
    if key not in args or args[key] is None:
        raise KeyError(key)
    return args[key]


def _active_for(ctx: ToolContext, caller_id: str) -> WorkingMemory | None:
    """Most recently registered in-flight call for this caller, if any."""
    for wm in reversed(list(ctx.active_calls.values())):
        if wm.caller_id == caller_id:
            return wm
    return None


def _sweep(ctx: ToolContext) -> None:
    cutoff = time.time() - _CALL_TTL_S
    stale = [cid for cid, wm in ctx.active_calls.items()
             if wm.created_at < cutoff]
    for cid in stale:
        del ctx.active_calls[cid]


def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """Execute one tool call. Names match schemas/<name>.json exactly."""
    try:
        if name == "memory_load":
            caller = _req(args, "caller_id")
            _sweep(ctx)
            wm = _active_for(ctx, caller)
            if wm is None:
                wm = WorkingMemory(store_id=ctx.store_id,
                                   call_id=f"call_{uuid.uuid4().hex[:12]}",
                                   caller_id=caller)
                ctx.active_calls[wm.call_id] = wm
            wm.current_slots = ctx.store.get_current(ctx.store_id, caller)
            wm.rules = ctx.store.get_rules(ctx.store_id)
            return {
                "call_id": wm.call_id,
                "current_slots": wm.current_slots,
                "rules": wm.rules,
            }
        if name == "memory_search":
            caller = _req(args, "caller_id")
            query = _req(args, "query")
            k = max(1, min(int(args.get("k", 3)), 10))
            results = ctx.store.search_history(ctx.store_id, caller, query, k=k)
            wm = _active_for(ctx, caller)
            if wm is not None:
                wm.tool_results.append(
                    {"tool": "memory_search", "query": query,
                     "hits": len(results)})
            return {"results": results}
        if name == "booking_reserve":
            slot_id = _req(args, "slot_id")
            caller = _req(args, "caller_id")
            party = int(args.get("party_size") or 0)
            if not 1 <= party <= 20:
                return {"ok": False, "error": "party_size must be 1-20"}
            res = ctx.slots.reserve(ctx.store_id, slot_id, caller, party)
            wm = _active_for(ctx, caller)
            if wm is not None:
                wm.tool_results.append({"tool": "booking_reserve", "result": res})
                if res["ok"]:
                    wm.confirmed["booking"] = {
                        "slot_id": res["slot_id"],
                        "party_size": party,
                        "status": "confirmed",
                    }
                else:
                    wm.unresolved.append(
                        f"slot {slot_id} gone; offered "
                        f"{', '.join(res['alternatives'])}")
            if not res["ok"]:
                res["note"] = "slot taken — offer alternatives"
            return res
        if name == "report_emit":
            call_id = _req(args, "call_id")
            wm = ctx.active_calls.pop(call_id, None)
            if wm is None:
                return {"ok": False,
                        "error": "unknown or already-closed call_id"}
            from ..memory.extract import DeterministicExtractor
            report = run_handoff(ctx.store, wm, DeterministicExtractor(),
                                 report_builder=build_report)
            validate_report(report)
            return {"ok": True, "report": report}
        return {"ok": False, "error": f"unknown tool: {name}"}
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
