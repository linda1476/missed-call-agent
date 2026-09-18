"""Owner-facing action-unit report. One call -> one report, schema-validated
against schemas/report_output.json."""

import json
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "report_output.json"


def build_report(wm, extractor) -> dict:
    """Build the report dict from a finished call's working memory."""
    b = wm.confirmed.get("booking")
    category = extractor.categorize(wm)

    action_items: list[dict] = []
    if category == "booking_confirmed":
        action_items.append({"kind": "none", "detail": "No action needed — booking confirmed."})
    elif category == "booking_cancelled":
        action_items.append({"kind": "none", "detail": "Caller cancelled their booking — slot released."})
    elif category == "callback_needed":
        action_items.append({"kind": "callback",
                             "detail": "; ".join(wm.unresolved) or "Caller needs a callback."})
    elif category == "booking_failed":
        action_items.append({"kind": "follow_up",
                             "detail": "Requested slot was taken; alternatives were offered."})
    elif category == "spam":
        action_items.append({"kind": "none", "detail": "Spam call filtered."})
    else:
        action_items.append({"kind": "review_rule",
                             "detail": "General inquiry — no booking change."})

    if b:
        verb = "cancelled" if b.get("status") == "cancelled" else "booked"
        summary = (f"{wm.caller_id}: {verb} {b['slot_id']} for "
                   f"{b['party_size']} ({b.get('name', 'guest')}).")
    elif wm.unresolved:
        summary = f"{wm.caller_id}: needs callback — {'; '.join(wm.unresolved)}."
    else:
        summary = f"{wm.caller_id}: {category}."

    return {
        "call_id": wm.call_id,
        "caller_id": wm.caller_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "action_items": action_items,
        "summary": summary,
        "booking": ({
            "slot_id": b["slot_id"],
            "party_size": int(b["party_size"]),
            "status": "confirmed" if b.get("status", "confirmed") == "confirmed" else b["status"],
        } if b else None),
    }


def validate_report(report: dict) -> bool:
    """Validate against schemas/report_output.json. Raises on invalid."""
    import jsonschema
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(report, schema)
    return True
