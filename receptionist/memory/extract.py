"""Post-call extraction. Default is deterministic (no paid LLM calls — see
T0-3: LLM Gateway tokens are billed separately and not covered by free
credits). An LLM extractor can be dropped in behind the same protocol."""

import re
from datetime import date, timedelta
from typing import Protocol

from .working import WorkingMemory

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]
_NUM_TOK = (r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve)")
_TIME_RES = (
    re.compile(rf"\b({_NUM_TOK})(?::(\d{{2}}))?\s*([ap]\.?m\.?)\b"),
    re.compile(rf"\b(?:at|around)\s+({_NUM_TOK})(?::(\d{{2}}))?\b"),
    re.compile(r"\b(\d{1,2}):(\d{2})\b"),
)
_BARE_NUMBER = re.compile(rf"\b({_NUM_TOK})\b", re.I)

# Words that commonly follow self-intro patterns but are never names —
# "i'm sorry", "it's fine", "this is ridiculous" are not callers' names.
_NAME_STOP = {
    "a", "an", "the", "this", "that", "there", "here", "back", "again",
    "me", "you", "my", "your", "our", "their", "his", "her",
    "fine", "ok", "okay", "good", "great", "sure", "sorry", "not", "so",
    "very", "really", "too", "also", "still", "just", "calling", "looking",
    "busy", "late", "early", "new", "old", "done", "ready", "free",
    "ridiculous", "crazy", "bad", "terrible", "awful", "expensive",
    "cheap", "urgent", "wrong", "wondering", "asking", "hoping", "trying",
    "yes", "no", "yeah", "nope", "about", "for", "to", "in", "on", "at",
}


def _num(text: str) -> int | None:
    text = text.lower().strip()
    if text.isdigit():
        return int(text)
    return _NUM_WORDS.get(text)


def unexplained_number(lower: str, fields: dict) -> int | None:
    """First bare number (1-20) in the utterance that no parsed field
    consumed — e.g. the 'five' in 'name's Dana, we're five' when only the
    name parsed. Digits attributed to time/date fields are explained away;
    word-times like 'noon' consume nothing."""
    explained = set(re.findall(r"\d+", fields.get("date_text", "")))
    if fields.get("time"):
        m = None
        for r in _TIME_RES:
            m = r.search(lower)
            if m:
                break
        if m:
            explained |= {g for g in m.groups() if g}
    for m in _BARE_NUMBER.finditer(lower):
        tok = m.group(1)
        if tok in explained:
            continue
        n = _num(tok)
        if n is not None and 1 <= n <= 20:
            return n
    return None


def parse_booking_fields(text: str) -> dict:
    """Pull booking fields from one utterance. Returns only found keys."""
    out: dict = {}
    t = " " + text.lower().strip() + " "

    # Strong patterns ("my name is X", "name is X") accept any case; weak
    # patterns ("i'm X", "this is X", "it's X") require X capitalized in the
    # original utterance so "it's fine"/"this is ridiculous" don't store a
    # fake name.
    m = re.search(r"(?:my name is|name is|name's)\s+([a-z']+)", t)
    if m and m.group(1) not in _NAME_STOP:
        out["name"] = m.group(1).title()
    else:
        m = re.search(r"(?:this is|it's|i am|i'm)\s+([a-z]+)", text, re.I)
        if m and m.group(1)[:1].isupper() and \
                m.group(1).lower() not in _NAME_STOP:
            out["name"] = m.group(1).title()

    m = re.search(rf"(?:table|party|reservation|booking|group)\s+(?:for|of)\s+"
                  rf"({_NUM_TOK})", t)
    if not m:
        m = re.search(rf"for\s+({_NUM_TOK})\s+"
                      r"(?:people|persons|guests|of us)", t)
    if not m:
        m = re.search(rf"for\s+({_NUM_TOK})\s*$", t)
    if m:
        out["party_size"] = _num(m.group(1))

    for i, day in enumerate(_WEEKDAYS):
        if re.search(rf"(?:this\s+|on\s+|next\s+)?{day}\b", t):
            out["weekday"] = day[:3]
            break
    m = re.search(r"\b(january|february|march|april|may|june|july|august|"
                  r"september|october|november|december)\s+(\d{1,2})", t)
    if m:
        out["date_text"] = f"{m.group(1)} {m.group(2)}"
    if "tomorrow" in t:
        out["day_offset"] = 1
    elif "today" in t or "tonight" in t:
        out["day_offset"] = 0

    if "noon" in t or "midday" in t:
        out["time"] = "1200"
    elif "midnight" in t:
        out["time"] = "0000"
    else:
        m = None
        for r in _TIME_RES:
            m = r.search(t)
            if m:
                break
        if m:
            hour = _num(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = (m.group(3) or "").replace(".", "") if m.lastindex >= 3 else ""
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            elif not ampm and 1 <= hour <= 7:
                hour += 12  # demo-domain bias: "at 7" ~ dinner, not 7 AM
            if 0 <= hour < 24 and 0 <= minute < 60:
                out["time"] = f"{hour:02d}{minute:02d}"
    return out


def fields_to_slot_id(fields: dict, today: date | None = None) -> str | None:
    """Map extracted fields onto the slot-table naming scheme <www>-<HHMM>.

    An explicit relative day ("tomorrow"/"tonight") beats an earlier weekday —
    it resolves against `today` (the shop's local date in production; the
    caller's machine date in tests). "September 20"-style dates can't map
    to a weekday without a year and stay unresolvable.
    """
    day = fields.get("weekday")
    if fields.get("day_offset") is not None:
        d = (today or date.today()) + timedelta(days=int(fields["day_offset"]))
        day = _WEEKDAYS[d.weekday()][:3]
    time_ = fields.get("time")
    if day and time_:
        return f"{day}-{time_}"
    return None


class Extractor(Protocol):
    def summarize(self, wm: WorkingMemory) -> str: ...
    def extract_facts(self, wm: WorkingMemory) -> dict: ...
    def categorize(self, wm: WorkingMemory) -> str: ...


class DeterministicExtractor:
    """Summarizes from structured working memory (confirmed fields, tool
    results) rather than re-parsing raw text — the same fields the production
    LLM fills via tool calls."""

    def summarize(self, wm: WorkingMemory) -> str:
        bits = [f"Call {wm.call_id} from {wm.caller_id}."]
        # bookings[] is the action log for the whole call — a caller may
        # hold several slots, so every one is recorded, not just the last.
        bookings = wm.confirmed.get("bookings")
        if not bookings and wm.confirmed.get("booking"):
            bookings = [wm.confirmed["booking"]]
        for b in bookings or []:
            status = b.get("status", "confirmed")
            verb = {"confirmed": "Booked", "cancelled": "Cancelled",
                    "moved": "Moved", "released": "Released"}.get(
                        status, str(status).title())
            bits.append(
                f"{verb} {b['slot_id']} for party of {b['party_size']}"
                + (f" ({b['name']})" if b.get("name") else "")
                + (f" — moved from {b['moved_from']}"
                   if b.get("moved_from") else "")
                + (f" — moved to {b['moved_to']}"
                   if b.get("moved_to") else "")
                + "."
            )
        for u in wm.unresolved:
            bits.append(f"Unresolved: {u}.")
        return " ".join(bits)

    def extract_facts(self, wm: WorkingMemory) -> dict:
        facts: dict = {}
        b = wm.confirmed.get("booking")
        if b:
            facts["last_booking_slot"] = b["slot_id"]
            facts["last_booking_party_size"] = str(b["party_size"])
            facts["last_booking_status"] = b.get("status", "confirmed")
        # A name given on any call — booking or pure inquiry — is durable.
        name = (b or {}).get("name") or wm.confirmed.get("caller_name")
        if name:
            facts["name"] = name
        if wm.unresolved:
            facts["pending"] = "; ".join(wm.unresolved)
        return facts

    def categorize(self, wm: WorkingMemory) -> str:
        b = wm.confirmed.get("booking")
        if b and b.get("status") == "cancelled":
            return "booking_cancelled"
        if b:
            return "booking_confirmed"
        # Set by the responder's telemarketing-pattern flag — a real caller
        # never has to say the word "spam" for the category to fire.
        if wm.confirmed.get("spam"):
            return "spam"
        if any("gone" in u for u in wm.unresolved):
            return "booking_failed"
        if wm.unresolved:
            return "callback_needed"
        return "inquiry"
