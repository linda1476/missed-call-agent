"""Deterministic dialogue manager for the booking domain.

In production the Voice Agent API's built-in LLM drives these decisions and
calls the same tools; this responder exists so the full call flow — memory
load, rule gating, CAS reservation — is testable offline and in CI, and it
backs the public /call/* session endpoints.

Important invariants:

- Confirmation gating is decided by procedural rules
  (rules.requires_confirmation), not by any model judgment. A caller can
  decline, correct, or ignore a confirmation prompt — only an affirmative
  answer books.
- The slot table is the source of truth for a caller's live bookings
  (held_by + status='reserved'). Memory slots record outcomes; the table
  decides what can be kept, changed, or cancelled — including bookings made
  earlier in the same call and callers holding more than one slot.
- Offered alternatives are tracked state: after a lost reservation the
  caller can pick an offered slot by name/time/ordinal, or confirm a lone
  offer outright — the offer is never a dead end.
"""

import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..booking.slots import SlotTable
from ..memory.extract import (
    fields_to_slot_id, parse_booking_fields, unexplained_number)
from ..memory.rules import requires_confirmation
from ..memory.working import WorkingMemory

_CONFIRM_RE = re.compile(
    r"\b(?:yes|yeah|yep|sure|please|correct|right|confirm|book it|"
    r"go ahead|sounds good|ok(?:ay)?|fine)\b", re.I)
_DECLINE_RE = re.compile(
    r"\b(?:no|nope|nah|not|don't|do not|never ?mind|cancel|stop|wait)\b",
    re.I)
_KEEP_RE = re.compile(
    r"\b(?:keep|sounds good|that'?s fine|all good|yes|correct|confirmed?|"
    r"fine)\b", re.I)
_CANCEL_WORDS = {"cancel", "cancellation", "remove", "delete"}
# "don't cancel", "I can't cancel", "do not cancel" — a negator within a
# short window before the cancel verb means the caller wants to KEEP it.
_NEGATED_CANCEL = re.compile(
    r"(?:don'?t|do not|didn'?t|did not|can'?t|cannot|won'?t|would not|"
    r"never|not)\b[^.!?;,]{0,30}\b(?:cancel|remov\w+|delet\w+)", re.I)
_CHANGE_WORDS = {"change", "move", "reschedule", "switch", "different"}
# "don't move it", "I can't change it" — same negation guard for changes.
_NEGATED_CHANGE = re.compile(
    r"(?:don'?t|do not|didn'?t|did not|can'?t|cannot|won'?t|would not|"
    r"never|not)\b[^.!?;,]{0,30}\b(?:change|move|reschedul\w+|switch)",
    re.I)
# Telemarketing/robocall phrases — flag the whole call as spam so the owner
# report's spam category is a deterministic signal, not a caller saying the
# literal word "spam".
_SPAM_RE = re.compile(
    r"\b(?:extended|auto|car)\s+warranty\b|\bwarranty\s+expir|"
    r"\byou(?:'ve|\s+have)\s+won\b|\bfree\s+cruise\b|"
    r"\bpress\s+(?:\d|one|two)\b|\blow[- ]interest\b|"
    r"\bcredit\s+card\s+debt\b|\btax\s+(?:refund|relief)\b|"
    r"\bmedicare\s+benefits\b|\bfinal\s+notice\b|"
    r"\bclaim\s+your\s+prize\b|\bsweepstakes\b|\bpre-?approved\b", re.I)
# An utterance with a question shape and no booking intent is an inquiry —
# it becomes an owner action item, not a silent detour into booking prompts.
_QUESTION_RE = re.compile(
    r"\?|^(?:do|does|are|is|can|could|may|what|when|where|who|which|why|"
    r"how)\b|\b(?:menu|hours|open|closed|parking|wifi|vegan|vegetarian|"
    r"gluten|allerg\w+|price|pricing|cost|deliver\w*|take ?out)\b", re.I)
_BOOK_INTENT_RE = re.compile(
    r"\b(?:book|booking|table|reserv\w*|party|seat|appointment|cancel|"
    r"move|change|reschedul\w*)\b", re.I)

# The three ways a caller states the day are mutually exclusive — the most
# recently stated one wins ("tomorrow" then "actually Friday" must resolve
# to Friday, not keep the stale day_offset).
_DAY_KEYS = ("weekday", "date_text", "day_offset")

_ORDINAL_WORDS = (("first", 0), ("1st", 0), ("second", 1), ("2nd", 1),
                  ("third", 2), ("3rd", 2), ("fourth", 3), ("4th", 3),
                  ("last", -1))
_ANY_WORDS = re.compile(r"\b(?:either|any|whichever|whatever)\b")


def _fmt_slot(slot_id: str) -> str:
    day, _, hm = slot_id.partition("-")
    return f"{day} at {hm[:2]}:{hm[2:]}"


def _list_slots(slot_ids: list[str]) -> str:
    parts = [_fmt_slot(s) for s in slot_ids]
    if len(parts) <= 2:
        return " or ".join(parts) if len(parts) == 2 else parts[0]
    return ", ".join(parts[:-1]) + f", or {parts[-1]}"


def _merge_fields(pending: dict, fields: dict) -> dict:
    """Merge parsed fields into the pending request, in place. A newly
    stated day expression clears the others — otherwise a stale day_offset
    shadows an explicit weekday correction and the wrong day books."""
    for k in _DAY_KEYS:
        if k in fields:
            for other in _DAY_KEYS:
                if other != k:
                    pending.pop(other, None)
    for k, v in fields.items():
        if v is not None:
            pending[k] = v
    return pending


def _match_slot_choice(candidates: list[str], lower: str, fields: dict,
                       today: date | None = None) -> str | None:
    """Map an utterance onto one of `candidates` (slot ids) — by full slot,
    partial day/time, or ordinal — or None when it doesn't pick one."""
    slot = fields_to_slot_id(fields, today=today)
    if slot is not None:
        return slot if slot in candidates else None
    day, tm = fields.get("weekday"), fields.get("time")
    if day or tm:
        hits = [c for c in candidates
                if (not day or c.split("-")[0] == day)
                and (not tm or c.split("-")[1] == tm)]
        return hits[0] if len(hits) == 1 else None
    for w, i in _ORDINAL_WORDS:
        if re.search(rf"\b{w}\b", lower):
            try:
                return candidates[i]
            except IndexError:
                return None
    if _ANY_WORDS.search(lower):
        return candidates[0] if candidates else None
    return None


def _shop_today() -> date:
    """'Tomorrow'/'tonight' resolve against the shop's local date — set
    SHOP_TZ (IANA name) on hosted deployments, else the server date."""
    tz_name = os.environ.get("SHOP_TZ", "").strip()
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            pass
    return date.today()


class Responder:
    def __init__(self, store_id: str, slots: SlotTable):
        self.store_id = store_id
        self.slots = slots

    # ---- live bookings: the slot table is the source of truth ----

    def _active_rows(self, wm: WorkingMemory) -> list[dict]:
        """Slots actually held by this caller right now — includes bookings
        made earlier in this call, excludes stale or cancelled memory."""
        rows = [r for r in self.slots.list(self.store_id)
                if r["status"] == "reserved" and r["held_by"] == wm.caller_id]
        rows.sort(key=lambda r: r["slot_id"])
        return rows

    # ---- greeting ----

    def greeting(self, wm: WorkingMemory) -> str:
        name = wm.current_slots.get("name", "")
        rows = self._active_rows(wm)
        if rows:
            descs = [f"{_fmt_slot(r['slot_id'])}, party of {r['party_size']}"
                     for r in rows]
            listing = (" and ".join(descs) if len(descs) <= 2
                       else ", ".join(descs[:-1]) + f", and {descs[-1]}")
            pronoun = "it" if len(rows) == 1 else "one"
            return (f"Thanks for calling back"
                    f"{', ' + name if name else ''}! I have you down for "
                    f"{listing}. Would you like to keep {pronoun}, "
                    f"change {pronoun}, or cancel?")
        if name:
            return (f"Thanks for calling back, {name}! "
                    f"What can I do for you today?")
        return ("Thanks for calling! I can take a booking or answer a "
                "question — what can I do for you?")

    # ---- cancel ----

    def _cancel(self, wm: WorkingMemory, slot_id: str) -> str:
        row = self.slots.get(self.store_id, slot_id) or {}
        if not self.slots.cancel(self.store_id, slot_id, wm.caller_id):
            return "I couldn't find an active booking to cancel."
        entry = next(
            (b for b in wm.confirmed.get("bookings", [])
             if b["slot_id"] == slot_id and b.get("status") == "confirmed"),
            None)
        if entry is None:
            entry = {
                "slot_id": slot_id,
                "party_size": int(row.get("party_size") or 0),
                "name": wm.confirmed.get("caller_name")
                        or wm.current_slots.get("name"),
                "status": "cancelled",
            }
            wm.confirmed.setdefault("bookings", []).append(entry)
        else:
            entry["status"] = "cancelled"
        wm.confirmed["booking"] = entry
        return f"Done — I've cancelled {_fmt_slot(slot_id)}."

    # ---- main turn ----

    def handle_utterance(self, wm: WorkingMemory, text: str) -> str:
        lower = text.lower()
        pending = dict(wm.confirmed.get("pending_booking") or {})
        fields = parse_booking_fields(text)
        today = _shop_today()
        held_rows = self._active_rows(wm)
        held = [r["slot_id"] for r in held_rows]

        # Telemarketing/robocall pitch — flag the whole call as spam so the
        # owner report's spam category can actually fire.
        if _SPAM_RE.search(lower):
            wm.confirmed["spam"] = True
            wm.confirmed.pop("pending_booking", None)
            return "Thanks — we're not interested. Goodbye."

        # Answering "which booking should I cancel — A or B?"
        if pending.get("cancel_of"):
            cands = pending["cancel_of"]
            pick = _match_slot_choice(cands, lower, fields, today)
            if pick:
                wm.confirmed.pop("pending_booking", None)
                return self._cancel(wm, pick)
            if _DECLINE_RE.search(lower) or _NEGATED_CANCEL.search(lower):
                wm.confirmed.pop("pending_booking", None)
                return "OK — I've left your bookings as they are."
            wm.confirmed["pending_booking"] = pending
            return f"Which one — {_list_slots(cands)}?"

        # Answering "which booking should I move — A or B?" The picked
        # source joins the request; stashed + new fields supply the target.
        if pending.get("change_pick"):
            cands = pending["change_pick"]
            pick = _match_slot_choice(cands, lower, fields, today)
            if pick:
                pending["change_from"] = pick
                pending.pop("change_pick")
                stashed = pending.pop("change_fields", None) or {}
                _merge_fields(pending, stashed)
                _merge_fields(pending, fields)
                row = self.slots.get(self.store_id, pick) or {}
                if "party_size" not in pending and row.get("party_size"):
                    pending["party_size"] = int(row["party_size"])
                if "name" not in pending:
                    nm = (wm.confirmed.get("caller_name")
                          or wm.current_slots.get("name"))
                    if nm:
                        pending["name"] = nm
                if not fields_to_slot_id(pending, today):
                    wm.confirmed["pending_booking"] = pending
                    return "Sure — what day and time works instead?"
                # fall through to the missing/confirm flow below
            elif _DECLINE_RE.search(lower):
                wm.confirmed.pop("pending_booking", None)
                return "OK — keeping your bookings as they are."
            else:
                wm.confirmed["pending_booking"] = pending
                return (f"Which booking should I move — "
                        f"{_list_slots(cands)}?")

        # Caller answering a confirmation prompt. Only an affirmative (or a
        # non-substantive detail like their name) books; a decline drops the
        # request; a substantive correction (slot or party size) updates the
        # request and re-asks; anything else re-asks. Nothing else may fall
        # through to _reserve.
        if pending.get("awaiting_confirm"):
            changed = {k: v for k, v in fields.items()
                       if v is not None and pending.get(k) != v}
            # A number the parser didn't consume is a party-size correction:
            # "name's Dana, we're actually five" must not book the old party.
            if "party_size" not in changed:
                n = unexplained_number(lower, fields)
                if n is not None and n != pending.get("party_size"):
                    changed["party_size"] = n
            merged = _merge_fields(dict(pending), changed)
            new_slot = fields_to_slot_id(merged, today)
            substantive = bool(changed) and (
                "party_size" in changed or
                "day_offset" in changed or "date_text" in changed or
                (new_slot is not None and new_slot != pending.get("slot_id")))
            if substantive:
                merged["awaiting_confirm"] = False
                pending = merged
            elif _DECLINE_RE.search(lower):
                wm.confirmed.pop("pending_booking", None)
                return ("No problem — I've left that off the books. "
                        "Anything else I can help with?")
            elif _CONFIRM_RE.search(lower) or changed:
                _merge_fields(pending, changed)
                return self._reserve(wm, pending)
            else:
                wm.confirmed["pending_booking"] = pending
                return (f"Sorry, I didn't catch that — shall I book the "
                        f"table for {pending['party_size']} on "
                        f"{_fmt_slot(pending['slot_id'])}?")

        # Answering an alternative offer after a lost reservation ("I can
        # offer sat at 20:00 — do any of those work?"). The offer is real
        # state: a pick refills slot_id and re-enters the confirm flow; a
        # decline drops it; a different request supersedes it.
        if pending.get("offered"):
            offered = pending["offered"]
            cand = fields_to_slot_id(fields, today)
            if cand is None:
                cand = _match_slot_choice(offered, lower, fields, today)
            if cand and cand in offered:
                pending["slot_id"] = cand
                pending.pop("offered")
                # Keep pending self-consistent: the pick IS the day/time —
                # and it IS the confirmation too, so don't re-ask "shall I
                # book it?" for a slot the caller just agreed to.
                d, _, hm = cand.partition("-")
                pending["weekday"] = d
                pending["time"] = hm
                pending["confirmed_offer"] = True
                pending.pop("date_text", None)
                pending.pop("day_offset", None)
                # fall through to missing/confirm below
            elif set(fields) - {"name"}:
                # Caller steered elsewhere ("how about Sunday instead?") —
                # the offer is moot; the new fields drive the request.
                pending.pop("offered")
                pending.pop("slot_id", None)
            elif len(offered) == 1 and _CONFIRM_RE.search(lower):
                cand = offered[0]
                pending["slot_id"] = cand
                pending.pop("offered")
                pending["confirmed_offer"] = True
                d, _, hm = cand.partition("-")
                pending["weekday"] = d
                pending["time"] = hm
                pending.pop("date_text", None)
                pending.pop("day_offset", None)
            elif _DECLINE_RE.search(lower):
                wm.confirmed.pop("pending_booking", None)
                return ("No problem — I've left that off the books. "
                        "Anything else I can help with?")
            else:
                wm.confirmed["pending_booking"] = pending
                return f"Which works for you — {_list_slots(offered)}?"

        # Affirming existing booking(s) ("keep it", "yes that's fine",
        # "Friday is fine" — restating a held day counts too). A pending
        # request (including a change intent) counts as abandoned.
        restates_held_day = bool(held) and set(fields) == {"weekday"} and \
            fields["weekday"] in {h.split("-")[0] for h in held}
        if held and _KEEP_RE.search(lower) and \
                (not fields or restates_held_day) and \
                not fields_to_slot_id(pending, today):
            wm.confirmed.pop("pending_booking", None)
            listing = " and ".join(_fmt_slot(h) for h in held)
            return f"Great — you're all set for {listing}. See you then!"

        # Explicit cancellation ("cancel", not "don't cancel"): a pending
        # request (and any change intent riding inside it) is dropped; an
        # active booking is released via CAS (only the holder can free it).
        # With several held slots the caller picks which — by day/time/ordinal.
        if any(w in lower for w in _CANCEL_WORDS) and \
                not _NEGATED_CANCEL.search(lower):
            if pending:
                wm.confirmed.pop("pending_booking", None)
                return "OK — I've dropped that booking request."
            if not held:
                return "I couldn't find an active booking to cancel."
            target = _match_slot_choice(held, lower, fields, today)
            if target is None and len(held) == 1:
                target = held[0]
            if target is None:
                pending["cancel_of"] = held
                wm.confirmed["pending_booking"] = pending
                return ("Which booking should I cancel — "
                        f"{_list_slots(held)}?")
            return self._cancel(wm, target)

        # Change request: the slot we're moving FROM rides inside the
        # pending request — it survives across turns and dies with the
        # request, so an abandoned change can't leak into a later unrelated
        # booking. The old slot is released only after the new one is
        # successfully reserved (in _reserve) — a failed change keeps the
        # caller's original booking.
        if any(w in lower for w in _CHANGE_WORDS) and held and \
                not pending and not _NEGATED_CHANGE.search(lower):
            src = _match_slot_choice(held, lower, fields, today)
            if src is None and len(held) == 1:
                src = held[0]
            if src is None:
                pending["change_pick"] = held
                pending["change_fields"] = fields
                wm.confirmed["pending_booking"] = pending
                return ("Which booking should I move — "
                        f"{_list_slots(held)}?")
            pending["change_from"] = src
            _merge_fields(pending, fields)
            # Moving keeps the source booking's party and name unless
            # restated — read party from the live row, name from memory.
            row = self.slots.get(self.store_id, src) or {}
            if "party_size" not in pending and row.get("party_size"):
                pending["party_size"] = int(row["party_size"])
            if "name" not in pending:
                prev_b = wm.confirmed.get("booking") or {}
                nm = ((prev_b.get("name")
                       if prev_b.get("slot_id") == src else None)
                      or wm.confirmed.get("caller_name")
                      or wm.current_slots.get("name"))
                if nm:
                    pending["name"] = nm
            if not (fields.get("weekday") or fields.get("time") or
                    fields.get("date_text") or
                    fields.get("day_offset") is not None):
                wm.confirmed["pending_booking"] = pending
                return "Sure — what day and time works instead?"

        # Pure inquiry (question shape, no booking intent): the
        # deterministic responder doesn't fake shop knowledge — it files
        # the question as an owner action item instead of forcing the
        # booking flow. Fields may have parsed (a weekday is still just a
        # question: "do you open Sundays?"); an in-flight request is kept
        # and its prompt re-asked after the question is filed.
        if _QUESTION_RE.search(lower) and \
                not _BOOK_INTENT_RE.search(lower):
            # An availability question names a slot — answer it from the
            # slot table instead of punting to the owner, and let the
            # caller roll straight into booking it.
            cand = fields_to_slot_id(fields, today)
            if not pending and cand is not None:
                row = self.slots.get(self.store_id, cand) or {}
                if fields.get("name"):
                    wm.confirmed["caller_name"] = fields["name"]
                if row.get("status") == "free":
                    pending.update(fields)
                    pending["slot_id"] = cand
                    wm.confirmed["pending_booking"] = pending
                    return (f"It is — {_fmt_slot(cand)} is open. "
                            f"For how many people?")
                alts = self.slots.alternatives(self.store_id, exclude=cand)
                if alts:
                    pending.update(fields)
                    pending.pop("slot_id", None)
                    pending["offered"] = alts
                    wm.confirmed["pending_booking"] = pending
                    return (f"That one's taken, but I can offer "
                            f"{_list_slots(alts)} — do any of those work?")
                wm.unresolved.append(f"availability: {cand} full")
                return ("That one's taken and I have no other openings — "
                        "can the owner call you back?")
            wm.unresolved.append(f"inquiry: {text.strip()}")
            if fields.get("name"):
                wm.confirmed["caller_name"] = fields["name"]
            ack = ("Good question — I've noted it for the owner, who'll "
                   "call you back with an answer.")
            if pending.get("awaiting_confirm"):
                return (ack + f" Now — shall I book the table for "
                        f"{pending['party_size']} on "
                        f"{_fmt_slot(pending['slot_id'])}?")
            if pending.get("offered"):
                return ack + (f" And — which works for you: "
                              f"{_list_slots(pending['offered'])}?")
            if pending:
                wm.confirmed["pending_booking"] = pending
                return ack + " Now, back to your booking — what day and " \
                    "time would you like?"
            return ack + " Anything else I can help with?"

        _merge_fields(pending, fields)

        slot_id = fields_to_slot_id(pending, today)
        if slot_id:
            pending["slot_id"] = slot_id
        elif any(k in fields for k in _DAY_KEYS):
            # Caller restated the day but it can't resolve to a slot (e.g.
            # a bare "September 25" with no year) — don't keep offering the
            # stale slot; ask for the day again.
            pending.pop("slot_id", None)
        if pending.get("name"):
            wm.confirmed["caller_name"] = pending["name"]

        # Asking for a slot they already hold (incl. "change to" their own
        # slot) is a no-op, not a failed reservation.
        if pending.get("slot_id") and pending["slot_id"] in held:
            wm.confirmed.pop("pending_booking", None)
            return (f"That's already your booking — "
                    f"{_fmt_slot(pending['slot_id'])}. Anything else?")

        missing = [k for k in ("slot_id", "party_size") if k not in pending]
        if missing:
            # Bare-answer fallback: when we asked "for how many?", a number
            # the field parser didn't consume is the party size — but
            # "Friday at 7 PM" must not read its hour as the party.
            if "party_size" in missing and fields_to_slot_id(pending, today):
                n = unexplained_number(lower, fields)
                if n is not None:
                    pending["party_size"] = n
                    missing.remove("party_size")
            if missing:
                if not pending and not fields:
                    # Nothing usable at all — ask openly instead of jumping
                    # to "for how many?" on a bare "hello".
                    return ("Sorry, I didn't catch that — are you calling "
                            "about a booking, or is there a question I can "
                            "take a message about?")
                wm.confirmed["pending_booking"] = pending
                if "party_size" in missing and "slot_id" not in missing:
                    return "For how many people?"
                return "What day and time would you like?"

        if requires_confirmation(wm.rules, pending.get("party_size")) \
                and not pending.get("awaiting_confirm") \
                and not pending.get("confirmed_offer"):
            pending["awaiting_confirm"] = True
            wm.confirmed["pending_booking"] = pending
            return (f"Just to confirm: a table for {pending['party_size']} "
                    f"on {_fmt_slot(pending['slot_id'])}"
                    + (f" for {pending['name']}" if pending.get("name") else "")
                    + " — shall I book it?")

        return self._reserve(wm, pending)

    def _reserve(self, wm: WorkingMemory, pending: dict) -> str:
        # The swap is atomic inside reserve(): the old slot is released in
        # the same transaction, after the new CAS succeeds — never before.
        change_from = pending.get("change_from")
        old_row = (self.slots.get(self.store_id, change_from) or {}
                   ) if change_from else {}
        res = self.slots.reserve(self.store_id, pending["slot_id"],
                                 wm.caller_id, int(pending["party_size"]),
                                 swap_from=change_from)
        if res["ok"]:
            wm.confirmed.pop("pending_booking", None)
            # A slot that was lost earlier in this request chain and has now
            # been satisfied by an accepted alternative is resolved — drop
            # its "gone" markers so the call isn't misreported as failed.
            for failed in pending.get("failed", []):
                wm.unresolved[:] = [
                    u for u in wm.unresolved if failed not in u]
            booking = {
                "slot_id": res["slot_id"],
                "party_size": int(pending["party_size"]),
                "name": pending.get("name"),
                "status": "confirmed",
            }
            if change_from and change_from != res["slot_id"]:
                if res.get("released_from") == change_from:
                    booking["moved_from"] = change_from
                    moved_entry = next(
                        (b for b in wm.confirmed.get("bookings", [])
                         if b["slot_id"] == change_from
                         and b.get("status") == "confirmed"), None)
                    if moved_entry is None:
                        # The released slot was booked on an earlier call —
                        # record it so the history log shows the full move.
                        wm.confirmed.setdefault("bookings", []).insert(0, {
                            "slot_id": change_from,
                            "party_size": int(old_row.get("party_size") or 0),
                            "name": booking.get("name"),
                            "status": "moved",
                            "moved_to": res["slot_id"],
                        })
                    else:
                        moved_entry["status"] = "moved"
                        moved_entry["moved_to"] = res["slot_id"]
                else:
                    wm.unresolved.append(
                        f"could not release {change_from} after move")
            wm.confirmed.setdefault("bookings", []).append(booking)
            wm.confirmed["booking"] = booking
            return (f"You're all set — {_fmt_slot(res['slot_id'])} for "
                    f"{pending['party_size']}"
                    + (f", {pending['name']}" if pending.get("name") else "")
                    + ". See you then!")
        wm.confirmed.pop("pending_booking", None)
        if res.get("reason") == "limit":
            wm.unresolved.append("caller hit the active-booking limit")
            return ("I'm sorry — you already have the maximum number of "
                    "active bookings with us. Anything else I can help "
                    "with?")
        wm.unresolved.append(f"slot {pending['slot_id']} gone; offered "
                             f"{', '.join(res['alternatives'])}")
        if res["alternatives"]:
            # Keep the request alive as an offer: party/name/change_from
            # carry over so a pick (or an accepted lone offer) reserves
            # without re-asking anything — and a move still releases the
            # old slot on success. `failed` records which slots lost the
            # race so a later successful pick can clear their markers.
            keep = {k: v for k, v in pending.items()
                    if k not in ("slot_id", "awaiting_confirm")}
            keep["offered"] = res["alternatives"]
            keep.setdefault("failed", []).append(pending["slot_id"])
            wm.confirmed["pending_booking"] = keep
            alts = ", ".join(_fmt_slot(a) for a in res["alternatives"])
            return (f"Sorry, that slot was just taken. I can offer {alts} "
                    f"— do any of those work?")
        return "Sorry, that slot was just taken and I have no openings left."
