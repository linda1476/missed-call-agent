"""Tool-call dispatch shared by the Voice Agent WebSocket client and the
HTTP tool server. Schemas live in schemas/ — the same files the agent's
session.update sends.

Call lifecycle through tools:

  memory_load(caller_id)   -> registers a WorkingMemory for the call and
                              returns its call_id plus the caller's
                              current-value slots + procedural rules
  memory_search(...)       -> append-only history lookup (logged to the call)
  booking_reserve(...)     -> CAS reservation; outcome recorded on the call
  booking_release(...)     -> CAS release of a slot the caller holds — the
                              cancel path, and the second half of a move
                              (reserve new first, then release old)
  report_emit(call_id)     -> closes the call: runs the fixed post-call
                              handoff (summary->history, facts->current,
                              state record, owner report) and drops the
                              working memory
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..booking.slots import SlotTable
from ..memory.extract import DeterministicExtractor
from ..memory.handoff import run_handoff
from ..memory.store import MemoryStore
from ..memory.working import WorkingMemory
from ..report.emit import build_report, validate_report

log = logging.getLogger("missed-call-agent")

_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"
_CALL_TTL_S = 3600  # hand off working memories older than an hour


@dataclass
class ToolContext:
    store: MemoryStore
    slots: SlotTable
    store_id: str
    active_calls: dict = None  # call_id -> WorkingMemory (in-flight)
    lock: threading.Lock = field(default_factory=threading.Lock)

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
    """Expire in-flight calls past the TTL. Spec: every call hands off then
    discards — a call the agent never closed via report_emit still runs
    summary -> facts -> state -> report, just late."""
    cutoff = time.time() - _CALL_TTL_S
    with ctx.lock:
        stale = [cid for cid, wm in ctx.active_calls.items()
                 if wm.created_at < cutoff]
        stale_wms = [ctx.active_calls.pop(cid) for cid in stale]
    for wm in stale_wms:
        try:
            run_handoff(ctx.store, wm, DeterministicExtractor(),
                        report_builder=build_report)
        except Exception:
            log.exception("handoff failed for swept call %s", wm.call_id)


def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """Execute one tool call. Names match schemas/<name>.json exactly."""
    try:
        if name == "memory_load":
            caller = _req(args, "caller_id")
            _sweep(ctx)
            with ctx.lock:
                wm = _active_for(ctx, caller)
                if wm is None:
                    wm = WorkingMemory(store_id=ctx.store_id,
                                       call_id=f"call_{uuid.uuid4().hex[:12]}",
                                       caller_id=caller)
                    ctx.active_calls[wm.call_id] = wm
            current = ctx.store.get_current(ctx.store_id, caller)
            rules = ctx.store.get_rules(ctx.store_id)
            # Live holds come from the slot table, not memory — a caller
            # may hold several slots and memory only tracks the latest.
            open_bookings = [
                {"slot_id": r["slot_id"], "party_size": r["party_size"]}
                for r in ctx.slots.list(ctx.store_id)
                if r["status"] == "reserved" and r["held_by"] == caller]
            with ctx.lock:
                wm.current_slots = current
                wm.rules = rules
                wm.open_bookings = open_bookings
            return {
                "call_id": wm.call_id,
                "current_slots": wm.current_slots,
                "rules": wm.rules,
                "open_bookings": wm.open_bookings,
            }
        if name == "memory_search":
            caller = _req(args, "caller_id")
            query = _req(args, "query")
            k = max(1, min(int(args.get("k", 3)), 10))
            results = ctx.store.search_history(ctx.store_id, caller, query, k=k)
            with ctx.lock:
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
            # swap_from marks a move: the old slot is released atomically
            # inside reserve() only after the new CAS succeeds — and only
            # if this caller actually holds it (defense in depth: a tool
            # caller can't free someone else's slot by naming it).
            swap_from = args.get("swap_from") or None
            if swap_from is not None:
                row = ctx.slots.get(ctx.store_id, swap_from) or {}
                if row.get("status") != "reserved" or \
                        row.get("held_by") != caller:
                    return {"ok": False,
                            "error": "swap_from is not a slot this caller "
                                     "holds"}
                if swap_from == slot_id:
                    return {"ok": True, "slot_id": slot_id,
                            "note": "already held by this caller"}
            old_row = (ctx.slots.get(ctx.store_id, swap_from) or {}
                       ) if swap_from else {}
            res = ctx.slots.reserve(ctx.store_id, slot_id, caller, party,
                                    swap_from=swap_from)
            with ctx.lock:
                wm = _active_for(ctx, caller)
                if wm is not None:
                    wm.tool_results.append(
                        {"tool": "booking_reserve", "result": res})
                if wm is None:
                    pass
                elif res["ok"]:
                    entry = {"slot_id": res["slot_id"], "party_size": party,
                             "status": "confirmed"}
                    bookings = wm.confirmed.setdefault("bookings", [])
                    if res.get("released_from"):
                        entry["moved_from"] = res["released_from"]
                        moved = next(
                            (b for b in bookings
                             if b["slot_id"] == res["released_from"]
                             and b.get("status") == "confirmed"), None)
                        if moved is None:
                            bookings.insert(0, {
                                "slot_id": res["released_from"],
                                "party_size": int(
                                    old_row.get("party_size") or 0),
                                "status": "moved", "moved_to": res["slot_id"]})
                        else:
                            moved["status"] = "moved"
                            moved["moved_to"] = res["slot_id"]
                    bookings.append(entry)
                    wm.confirmed["booking"] = entry
                elif res.get("reason") == "limit":
                    wm.unresolved.append("caller hit the active-booking "
                                         "limit")
                else:
                    wm.unresolved.append(
                        f"slot {slot_id} gone; offered "
                        f"{', '.join(res['alternatives'])}")
            if not res["ok"]:
                res["note"] = ("caller at booking limit"
                               if res.get("reason") == "limit"
                               else "slot taken — offer alternatives")
            return res
        if name == "booking_release":
            slot_id = _req(args, "slot_id")
            caller = _req(args, "caller_id")
            row = ctx.slots.get(ctx.store_id, slot_id)
            if not ctx.slots.cancel(ctx.store_id, slot_id, caller):
                return {"ok": False,
                        "error": "no active booking held by this caller"}
            with ctx.lock:
                wm = _active_for(ctx, caller)
                if wm is not None:
                    wm.tool_results.append(
                        {"tool": "booking_release", "slot_id": slot_id})
                    bookings = wm.confirmed.setdefault("bookings", [])
                    entry = next(
                        (b for b in bookings
                         if b["slot_id"] == slot_id
                         and b.get("status") == "confirmed"), None)
                    latest = wm.confirmed.get("booking")
                    latest_other = latest and \
                        latest.get("status") == "confirmed" and \
                        latest.get("slot_id") != slot_id
                    if latest_other and entry is None:
                        # Second half of a two-step move (reserve new ->
                        # release old): the caller's live booking is the NEW
                        # slot — annotate it instead of logging a
                        # cancellation. (The preferred move path is
                        # booking_reserve's swap_from, which swaps
                        # atomically; this keeps the legacy order
                        # consistent.)
                        latest["moved_from"] = slot_id
                        bookings.insert(0, {
                            "slot_id": slot_id,
                            "party_size": int(
                                (row or {}).get("party_size") or 0),
                            "status": "moved",
                            "moved_to": latest["slot_id"]})
                    else:
                        if entry is None:
                            entry = {
                                "slot_id": slot_id,
                                "party_size": int(
                                    (row or {}).get("party_size") or 0),
                                "status": "cancelled",
                            }
                            bookings.append(entry)
                        else:
                            entry["status"] = "cancelled"
                        wm.confirmed["booking"] = entry
            return {"ok": True, "slot_id": slot_id, "status": "released"}
        if name == "report_emit":
            call_id = _req(args, "call_id")
            with ctx.lock:
                wm = ctx.active_calls.pop(call_id, None)
            if wm is None:
                return {"ok": False,
                        "error": "unknown or already-closed call_id"}
            report = run_handoff(ctx.store, wm, DeterministicExtractor(),
                                 report_builder=build_report)
            try:
                validate_report(report)
            except Exception:
                log.exception("report schema validation failed for %s",
                              call_id)
                return {"ok": False,
                        "error": "internal report validation failed"}
            return {"ok": True, "report": report}
        return {"ok": False, "error": f"unknown tool: {name}"}
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
