"""Owner-facing action-unit report. One call -> one report, schema-validated
against schemas/report_output.json."""

import json
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "report_output.json"


def _booking_public(b: dict) -> dict:
    out = {"slot_id": b["slot_id"],
           "party_size": int(b["party_size"]),
           "status": b.get("status", "confirmed")}
    if b.get("moved_to"):
        out["moved_to"] = b["moved_to"]
    if b.get("moved_from"):
        out["moved_from"] = b["moved_from"]
    return out


def build_report(wm, extractor) -> dict:
    """Build the report dict from a finished call's working memory."""
    category = extractor.categorize(wm)
    # bookings[] is the call's full action log (a caller may hold or touch
    # several slots); confirmed["booking"] stays as the latest for compat.
    bookings = wm.confirmed.get("bookings")
    if not bookings and wm.confirmed.get("booking"):
        bookings = [wm.confirmed["booking"]]
    b = wm.confirmed.get("booking")

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

    if bookings:
        verbs = {"confirmed": "booked", "cancelled": "cancelled",
                 "moved": "moved off"}
        parts = []
        for e in bookings:
            s = (f"{verbs.get(e.get('status'), e.get('status'))} "
                 f"{e['slot_id']} for {e['party_size']}"
                 f" ({e.get('name') or 'guest'})")
            if e.get("moved_from"):
                s += f" [from {e['moved_from']}]"
            if e.get("moved_to"):
                s += f" [to {e['moved_to']}]"
            parts.append(s)
        summary = f"{wm.caller_id}: {'; '.join(parts)}."
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
        "booking": (_booking_public(b) if b else None),
        "bookings": [_booking_public(e) for e in (bookings or [])],
    }


def validate_report(report: dict) -> bool:
    """Validate against schemas/report_output.json. Raises on invalid."""
    import jsonschema
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(report, schema)
    return True
