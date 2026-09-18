"""Deterministic dialogue manager for the booking domain.

In production the Voice Agent API's built-in LLM drives these decisions and
calls the same tools; this responder exists so the full call flow — memory
load, rule gating, CAS reservation — is testable offline and in CI.

Important: confirmation gating is decided by procedural rules
(rules.requires_confirmation), not by any model judgment. A caller can
decline, correct, or ignore a confirmation prompt — only an affirmative
answer books.
"""

import re

from ..booking.slots import SlotTable
from ..memory.extract import _num, fields_to_slot_id, parse_booking_fields
from ..memory.rules import requires_confirmation
from ..memory.working import WorkingMemory

_CONFIRM_RE = re.compile(
    r"\b(?:yes|yeah|yep|sure|please|correct|right|confirm|book it|"
    r"go ahead|sounds good|ok(?:ay)?|fine)\b", re.I)
_DECLINE_RE = re.compile(
    r"\b(?:no|nope|nah|not|don't|do not|never ?mind|cancel|stop|wait)\b",
    re.I)
_KEEP_RE = re.compile(
    r"\b(?:keep|sounds good|that'?s fine|all good|yes|correct|confirmed?)\b",
    re.I)
_CANCEL_WORDS = {"cancel", "cancellation"}
_CHANGE_WORDS = {"change", "move", "reschedule", "switch", "different"}
_BARE_NUMBER = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b", re.I)


def _fmt_slot(slot_id: str) -> str:
    day, _, hm = slot_id.partition("-")
    return f"{day} at {hm[:2]}:{hm[2:]}"


class Responder:
    def __init__(self, store_id: str, slots: SlotTable):
        self.store_id = store_id
        self.slots = slots

    def _active_slot(self, wm: WorkingMemory) -> str | None:
        """The caller's live booking slot — a cancelled booking is not active."""
        if wm.current_slots.get("last_booking_status") == "cancelled":
            return None
        return wm.current_slots.get("last_booking_slot")

    def greeting(self, wm: WorkingMemory) -> str:
        name = wm.current_slots.get("name", "")
        last = self._active_slot(wm)
        if last:
            party = wm.current_slots.get("last_booking_party_size", "?")
            return (f"Thanks for calling back"
                    f"{', ' + name if name else ''}! I have you down for "
                    f"{_fmt_slot(last)}, party of {party}. "
                    f"Would you like to keep it, change it, or cancel?")
        if name:
            return (f"Thanks for calling back, {name}! "
                    f"What can I do for you today?")
        return ("Thanks for calling! I can take a booking or answer a "
                "question — what can I do for you?")

    def handle_utterance(self, wm: WorkingMemory, text: str) -> str:
        lower = text.lower()
        pending = dict(wm.confirmed.get("pending_booking") or {})

        # Caller answering a confirmation prompt. Only an affirmative (or a
        # non-substantive detail like their name) books; a decline drops the
        # request; a substantive correction (slot or party size) updates the
        # request and re-asks; anything else re-asks. Nothing else may fall
        # through to _reserve.
        if pending.get("awaiting_confirm"):
            fields = parse_booking_fields(text)
            changed = {k: v for k, v in fields.items()
                       if v is not None and pending.get(k) != v}
            if not changed and not fields:
                m = _BARE_NUMBER.search(lower)
                if m and _num(m.group(1)) != pending.get("party_size"):
                    changed = {"party_size": _num(m.group(1))}
            merged = {**pending, **changed}
            new_slot = fields_to_slot_id(merged)
            substantive = bool(changed) and (
                "party_size" in changed or
                "day_offset" in changed or "date_text" in changed or
                (new_slot is not None and new_slot != pending.get("slot_id")))
            if substantive:
                pending.update(changed)
                pending["awaiting_confirm"] = False
            elif _DECLINE_RE.search(lower):
                wm.confirmed.pop("pending_booking", None)
                return ("No problem — I've left that off the books. "
                        "Anything else I can help with?")
            elif _CONFIRM_RE.search(lower) or changed:
                pending.update(changed)
                return self._reserve(wm, pending)
            else:
                wm.confirmed["pending_booking"] = pending
                return (f"Sorry, I didn't catch that — shall I book the "
                        f"table for {pending['party_size']} on "
                        f"{_fmt_slot(pending['slot_id'])}?")

        # Affirming the existing booking ("keep it", "yes that's fine") with
        # no pending request — nothing new to reserve.
        if not pending and self._active_slot(wm) and \
                _KEEP_RE.search(lower) and not parse_booking_fields(text):
            return (f"Great — you're all set for "
                    f"{_fmt_slot(self._active_slot(wm))}. See you then!")

        # Explicit cancellation: a pending request is dropped; an active
        # booking is released via CAS (only the holder can free it).
        if any(w in lower for w in _CANCEL_WORDS):
            if pending:
                wm.confirmed.pop("pending_booking", None)
                return "OK — I've dropped that booking request."
            last = self._active_slot(wm)
            if last and self.slots.cancel(self.store_id, last, wm.caller_id):
                prev = wm.current_slots.get("last_booking_party_size")
                wm.confirmed["booking"] = {
                    "slot_id": last,
                    "party_size": int(prev) if str(prev).isdigit() else 0,
                    "status": "cancelled",
                }
                wm.confirmed.pop("change_from", None)
                return f"Done — I've cancelled {_fmt_slot(last)}."
            return "I couldn't find an active booking to cancel."

        # Change request: remember which slot we're moving FROM, then gather
        # the new request. The old slot is released only after the new one
        # is successfully reserved (in _reserve) — a failed change keeps the
        # caller's original booking.
        if any(w in lower for w in _CHANGE_WORDS) and \
                self._active_slot(wm) and not pending:
            fields = parse_booking_fields(text)
            if not (fields.get("weekday") or fields.get("time") or
                    fields.get("date_text") or fields.get("day_offset") is not None):
                return "Sure — what day and time works instead?"
            wm.confirmed["change_from"] = self._active_slot(wm)
            pending.update({k: v for k, v in fields.items()
                            if v is not None})
            # Changing a booking keeps the party unless restated.
            prev = wm.current_slots.get("last_booking_party_size")
            if "party_size" not in pending and prev and str(prev).isdigit():
                pending["party_size"] = int(prev)

        fields = parse_booking_fields(text)
        pending.update({k: v for k, v in fields.items() if v is not None})

        slot_id = fields_to_slot_id(pending)
        if slot_id:
            pending["slot_id"] = slot_id
        if pending.get("name"):
            wm.confirmed["caller_name"] = pending["name"]

        # Asking for the slot they already hold (incl. "change to" their own
        # slot) is a no-op, not a failed reservation.
        if pending.get("slot_id") and \
                pending["slot_id"] == self._active_slot(wm):
            wm.confirmed.pop("pending_booking", None)
            wm.confirmed.pop("change_from", None)
            return (f"That's already your booking — "
                    f"{_fmt_slot(pending['slot_id'])}. Anything else?")

        missing = [k for k in ("slot_id", "party_size") if k not in pending]
        if missing:
            # Bare-answer fallback: when we asked "for how many?", a bare
            # "two"/"4" is the party size, not a new request. Only when the
            # utterance parsed to nothing — "Friday at 7 PM" must not read
            # its hour as the party.
            if "party_size" in missing and fields_to_slot_id(pending) \
                    and not fields:
                m = _BARE_NUMBER.search(lower)
                if m:
                    pending["party_size"] = _num(m.group(1))
                    missing.remove("party_size")
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
            booking = {
                "slot_id": res["slot_id"],
                "party_size": int(pending["party_size"]),
                "name": pending.get("name"),
                "status": "confirmed",
            }
            # A successful change swaps slots: release the old one only now
            # that the new reservation is confirmed.
            change_from = wm.confirmed.pop("change_from", None)
            if change_from and change_from != res["slot_id"] and \
                    self.slots.cancel(self.store_id, change_from, wm.caller_id):
                booking["moved_from"] = change_from
            wm.confirmed["booking"] = booking
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
