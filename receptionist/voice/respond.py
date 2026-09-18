"""Deterministic dialogue manager for the booking domain.

In production the Voice Agent API's built-in LLM drives these decisions and
calls the same tools; this responder exists so the full call flow — memory
load, rule gating, CAS reservation — is testable offline and in CI.

Important: confirmation gating is decided by procedural rules
(rules.requires_confirmation), not by any model judgment.
"""

from ..booking.slots import SlotTable
from ..memory.extract import fields_to_slot_id, parse_booking_fields
from ..memory.rules import requires_confirmation
from ..memory.working import WorkingMemory

_CONFIRM_WORDS = {"yes", "yeah", "yep", "sure", "please", "correct",
                  "right", "confirm", "book it", "go ahead", "sounds good"}
_CANCEL_WORDS = {"cancel", "cancellation"}
_CHANGE_WORDS = {"change", "move", "reschedule", "switch", "different"}


def _fmt_slot(slot_id: str) -> str:
    day, _, hm = slot_id.partition("-")
    return f"{day} at {hm[:2]}:{hm[2:]}"


class Responder:
    def __init__(self, store_id: str, slots: SlotTable):
        self.store_id = store_id
        self.slots = slots

    def greeting(self, wm: WorkingMemory) -> str:
        last = wm.current_slots.get("last_booking_slot")
        if last:
            party = wm.current_slots.get("last_booking_party_size", "?")
            name = wm.current_slots.get("name", "")
            return (f"Thanks for calling back"
                    f"{', ' + name if name else ''}! I have you down for "
                    f"{_fmt_slot(last)}, party of {party}. "
                    f"Would you like to keep it, change it, or cancel?")
        return ("Thanks for calling! I can take a booking or answer a "
                "question — what can I do for you?")

    def handle_utterance(self, wm: WorkingMemory, text: str) -> str:
        lower = text.lower()

        if any(w in lower for w in _CANCEL_WORDS):
            last = wm.current_slots.get("last_booking_slot")
            if last and self.slots.cancel(self.store_id, last, wm.caller_id):
                wm.confirmed["booking"] = {"slot_id": last, "party_size": 0,
                                           "status": "cancelled"}
                return f"Done — I've cancelled {_fmt_slot(last)}."
            return "I couldn't find an active booking to cancel."

        pending = dict(wm.confirmed.get("pending_booking") or {})

        # Caller answering a confirmation prompt.
        if pending.get("awaiting_confirm") and \
                any(w in lower for w in _CONFIRM_WORDS):
            return self._reserve(wm, pending)

        # Change request: free the old slot first, then keep parsing the
        # same utterance for new fields.
        if any(w in lower for w in _CHANGE_WORDS) and \
                wm.current_slots.get("last_booking_slot") and not pending:
            last = wm.current_slots["last_booking_slot"]
            if self.slots.cancel(self.store_id, last, wm.caller_id):
                wm.confirmed["booking"] = {"slot_id": last, "party_size": 0,
                                           "status": "cancelled"}
            fields = parse_booking_fields(text)
            if not fields_to_slot_id(fields):
                return "Sure — what day and time works instead?"
            pending.update(fields)
            # Changing a booking keeps the party unless restated.
            prev = wm.current_slots.get("last_booking_party_size")
            if "party_size" not in pending and prev and prev.isdigit():
                pending["party_size"] = int(prev)

        fields = parse_booking_fields(text)
        pending.update({k: v for k, v in fields.items() if v is not None})

        slot_id = fields_to_slot_id(pending)
        if slot_id:
            pending["slot_id"] = slot_id
        if pending.get("name"):
            wm.confirmed["caller_name"] = pending["name"]

        missing = [k for k in ("slot_id", "party_size") if k not in pending]
        if missing:
            wm.confirmed["pending_booking"] = pending
            if "party_size" in missing:
                return "For how many people?"
            return "What day and time would you like?"

        if requires_confirmation(wm.rules, pending.get("party_size")) \
                and not pending.get("awaiting_confirm"):
            pending["awaiting_confirm"] = True
            wm.confirmed["pending_booking"] = pending
            return (f"Just to confirm: a table for {pending['party_size']} "
                    f"on {_fmt_slot(pending['slot_id'])}"
                    + (f" for {pending['name']}" if pending.get("name") else "")
                    + " — shall I book it?")

        return self._reserve(wm, pending)

    def _reserve(self, wm: WorkingMemory, pending: dict) -> str:
        res = self.slots.reserve(self.store_id, pending["slot_id"],
                                 wm.caller_id, int(pending["party_size"]))
        wm.confirmed.pop("pending_booking", None)
        if res["ok"]:
            wm.confirmed["booking"] = {
                "slot_id": res["slot_id"],
                "party_size": int(pending["party_size"]),
                "name": pending.get("name"),
                "status": "confirmed",
            }
            return (f"You're all set — {_fmt_slot(res['slot_id'])} for "
                    f"{pending['party_size']}"
                    + (f", {pending['name']}" if pending.get("name") else "")
                    + ". See you then!")
        wm.unresolved.append(f"slot {pending['slot_id']} gone; offered "
                             f"{', '.join(res['alternatives'])}")
        if res["alternatives"]:
            alts = ", ".join(_fmt_slot(a) for a in res["alternatives"])
            return (f"Sorry, that slot was just taken. I can offer {alts} "
                    f"— do any of those work?")
        return "Sorry, that slot was just taken and I have no openings left."
