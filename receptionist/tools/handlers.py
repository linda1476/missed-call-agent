"""Tool-call dispatch shared by the Voice Agent WebSocket client and the
HTTP tool server. Schemas live in schemas/ — the same files the agent's
session.update sends."""

import json
from dataclasses import dataclass
from pathlib import Path

from ..booking.slots import SlotTable
from ..memory.store import MemoryStore
from ..report.emit import build_report, validate_report

_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"


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


def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """Execute one tool call. Names match schemas/<name>.json exactly."""
    if name == "memory_load":
        caller = args["caller_id"]
        return {
            "current_slots": ctx.store.get_current(ctx.store_id, caller),
            "rules": ctx.store.get_rules(ctx.store_id),
        }
    if name == "memory_search":
        return {
            "results": ctx.store.search_history(
                ctx.store_id, args["caller_id"], args["query"],
                k=int(args.get("k", 3))),
        }
    if name == "booking_reserve":
        res = ctx.slots.reserve(ctx.store_id, args["slot_id"],
                                args["caller_id"], int(args["party_size"]))
        if not res["ok"]:
            res["note"] = "slot taken — offer alternatives"
        return res
    if name == "report_emit":
        wm = (ctx.active_calls or {}).get(args["call_id"])
        if wm is None:
            return {"ok": False, "error": "unknown or already-closed call_id"}
        from ..memory.extract import DeterministicExtractor
        report = build_report(wm, DeterministicExtractor())
        validate_report(report)
        return {"ok": True, "report": report}
    return {"ok": False, "error": f"unknown tool: {name}"}
