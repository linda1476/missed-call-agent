"""Deterministic reply guard — the spec's contradiction check.

"모델 응답이 현재값과 모순되면 코드가 잡아 재생성한다" — when an agent
reply contradicts current values, code catches it and regenerates. This is
a deterministic comparison against the slot table (the source of truth for
live bookings), not model judgment:

- a reply that asserts a booking names a slot the caller does NOT hold
  → contradiction; the corrected reply names the real one (or admits the
  booking didn't happen if nothing is held).
- a reply that asserts a cancellation for a slot the caller STILL holds
  → contradiction; the corrected reply says it stayed on the books.
- a confirmed-booking claim stating a party size different from the
  confirmed record → contradiction.

Wire it after reply generation (pipeline.turn does; in production it can
also run on `transcript.agent` events and trigger a reply.create
correction — see session.py's reply_guard hook).
"""

import re

# Assertion phrases — questions and offers ("shall I book it?", "I can
# offer...") don't match these, so prompts are never flagged as claims.
_BOOKED_RE = re.compile(
    r"\byou'?re all set\b|\ball set\b|\bbooked\b|\bconfirmed\b|"
    r"\breservation (?:is )?(?:made|confirmed|booked)\b|"
    r"\bi'?ve (?:got|put|booked|reserved) you\b", re.I)
_CANCELLED_RE = re.compile(
    r"\bi'?ve cancell?ed\b|\bcancell?ed\b|\boff the books\b|"
    r"\breleased\b|\bremoved your\b", re.I)
_SLOT_REF = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)\s+at\s+(\d{1,2}):(\d{2})\b", re.I)
_PARTY_REF = re.compile(r"\b(?:for|party of)\s+(\d{1,2})\b", re.I)


def _fmt(slot_id: str) -> str:
    day, _, hm = slot_id.partition("-")
    return f"{day} at {hm[:2]}:{hm[2:]}"


def _mentioned_slots(text: str) -> list[str]:
    return [f"{m.group(1)}-{int(m.group(2)):02d}{m.group(3)}"
            for m in _SLOT_REF.finditer(text)]


def check_reply(reply: str, wm, slots, store_id: str) -> dict:
    """Compare a reply against current state. Returns
    {ok, issues, corrected} — `corrected` is a truthful replacement
    sentence when a contradiction is found, else None."""
    issues: list[str] = []
    corrected: str | None = None
    held = {r["slot_id"]: r for r in slots.list(store_id)
            if r["status"] == "reserved" and r["held_by"] == wm.caller_id}
    lower = reply.lower()
    mentioned = _mentioned_slots(reply)

    if _BOOKED_RE.search(lower):
        b = wm.confirmed.get("booking") or {}
        truth = b["slot_id"] if b.get("status") == "confirmed" else None
        claimed = mentioned[-1] if mentioned else None
        if claimed and claimed not in held:
            issues.append(
                f"claims booking {claimed} but caller does not hold it")
        elif claimed is None and not held:
            issues.append("claims a booking but caller holds none")
        if issues:
            if held:
                slot = truth if truth in held else sorted(held)[0]
                row = held[slot]
                corrected = (
                    f"You're all set — {_fmt(slot)} for "
                    f"{row.get('party_size') or b.get('party_size')}"
                    + (f", {b['name']}" if b.get("name") else "")
                    + ". See you then!")
            else:
                corrected = ("I'm sorry — I wasn't able to confirm that "
                             "booking after all. Would you like me to try "
                             "a different time?")
        else:
            m = _PARTY_REF.search(reply)
            if m and truth is not None:
                real_party = b.get("party_size")
                if real_party is not None and \
                        int(m.group(1)) != int(real_party):
                    issues.append(
                        f"claims party of {m.group(1)} but booking is "
                        f"for {real_party}")
                    corrected = (
                        f"You're all set — {_fmt(truth)} for "
                        f"{real_party}. See you then!")

    if _CANCELLED_RE.search(lower):
        # A cancellation claim for a slot still held is a lie; check the
        # mentioned slots and the cancelled record's slot alike.
        suspects = list(mentioned)
        b = wm.confirmed.get("booking") or {}
        if not suspects and b.get("status") == "cancelled":
            suspects = [b["slot_id"]]
        for s in suspects:
            if s in held:
                issues.append(f"claims {s} cancelled but it is still held")
                corrected = (f"I'm sorry — {_fmt(s)} is still on the "
                             f"books; I wasn't able to cancel it.")

    return {"ok": not issues, "issues": issues, "corrected": corrected}


def enforce_reply(reply: str, wm, slots, store_id: str) -> str:
    """Return the reply, replaced by the corrected version on
    contradiction — deterministic regeneration, per spec."""
    return check_reply(reply, wm, slots, store_id)["corrected"] or reply


def correction_instructions(check: dict) -> str | None:
    """Phrase a failed check as reply.create instructions for the
    production Voice Agent session (session.py reply_guard hook)."""
    if check["ok"]:
        return None
    detail = check["corrected"] or "; ".join(check["issues"])
    return ("Your previous reply contradicted the actual booking state. "
            f"Tell the caller instead: {detail}")


def make_reply_guard(wm, slots, store_id: str):
    """Bind the check to one call's state for session.py's reply_guard
    hook: callable(transcript) -> correction instructions | None."""
    def _guard(transcript: str) -> str | None:
        return correction_instructions(
            check_reply(transcript, wm, slots, store_id))
    return _guard
