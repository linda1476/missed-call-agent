"""Post-call extraction. Default is deterministic (no paid LLM calls — see
T0-3: LLM Gateway tokens are billed separately and not covered by free
credits). An LLM extractor can be dropped in behind the same protocol."""

import re
from typing import Protocol

from .working import WorkingMemory

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _num(text: str) -> int | None:
    text = text.lower().strip()
    if text.isdigit():
        return int(text)
    return _NUM_WORDS.get(text)


def parse_booking_fields(text: str) -> dict:
    """Pull booking fields from one utterance. Returns only found keys."""
    out: dict = {}
    t = " " + text.lower().strip() + " "

    m = re.search(r"(?:my name is|name is|this is|name's|it's|i am|i'm)\s+([a-z]+)", t)
    if m and m.group(1) not in {"calling", "looking", "here", "just", "a", "an"}:
        out["name"] = m.group(1).title()

    m = re.search(r"(?:table|party|reservation|booking|group)\s+(?:for|of)\s+"
                  r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)", t)
    if not m:
        m = re.search(r"for\s+(\d+|two|three|four|five|six|seven|eight)\s+"
                      r"(?:people|persons|guests|of us)", t)
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
        m = (re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)\b", t)
             or re.search(r"\b(?:at|around)\s+(\d{1,2})(?::(\d{2}))?\b", t)
             or re.search(r"\b(\d{1,2}):(\d{2})\b", t))
        if m:
            hour = int(m.group(1))
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


def fields_to_slot_id(fields: dict) -> str | None:
    """Map extracted fields onto the slot-table naming scheme <www>-<HHMM>."""
    day = fields.get("weekday")
    time = fields.get("time")
    if day and time:
        return f"{day}-{time}"
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
        if wm.confirmed.get("booking"):
            b = wm.confirmed["booking"]
            bits.append(
                f"Booked {b['slot_id']} for party of {b['party_size']}"
                + (f" ({b['name']})" if b.get("name") else "") + "."
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
            if b.get("name"):
                facts["name"] = b["name"]
            facts["last_booking_status"] = b.get("status", "confirmed")
        if wm.unresolved:
            facts["pending"] = "; ".join(wm.unresolved)
        return facts

    def categorize(self, wm: WorkingMemory) -> str:
        if wm.confirmed.get("booking"):
            return "booking_confirmed"
        if any("spam" in r.lower() for r in wm.requests):
            return "spam"
        if wm.unresolved:
            return "callback_needed"
        return "inquiry"
