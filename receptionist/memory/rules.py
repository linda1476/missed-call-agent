"""Compile owner-correction rule text into deterministic predicates.

The owner types free text ("auto-book parties of 4 or fewer without
confirming"); we compile the demo-domain patterns we support into
machine-checkable predicates. Unknown phrasings stay advisory-only
(shown to the agent as text) and never silently take effect.
"""

import re

_AUTO_BOOK = re.compile(
    r"(?:auto[- ]?book|just book|book directly|no confirmation|"
    r"without (?:asking|confirming)|skip confirm)", re.I)
# Inclusive bounds: "parties of 4 or fewer/less/under", "up to 4",
# "at most 4", "no more than 4", "<= 4" — N itself is allowed.
_PARTY_LTE = re.compile(
    r"part(?:y|ies)\s+of\s+(\d+)\s+or\s+(?:less|fewer|under)|"
    r"(?:up to|at most|no more than|max(?:imum)?(?: of)?|<=)\s*(\d+)"
    r"\s*(?:people|guests|persons)?", re.I)
# Exclusive bounds: "under 5", "fewer than 5", "less than 5", "< 5" —
# N itself is NOT allowed, so the max is N-1.
_PARTY_LT = re.compile(
    r"(?:under|fewer than|less than|below|smaller than|<)\s*(\d+)"
    r"\s*(?:people|guests|persons)?", re.I)


def auto_book_max_party(rule_text: str) -> int | None:
    """If the rule says 'auto-book parties of N or less', return N.

    Exclusive phrasings ("under 5") yield N-1 — the owner's stated bound
    is never exceeded."""
    if not _AUTO_BOOK.search(rule_text):
        return None
    m = _PARTY_LTE.search(rule_text)
    if m:
        return int(m.group(1) or m.group(2))
    m = _PARTY_LT.search(rule_text)
    if m:
        return max(0, int(m.group(1)) - 1)
    return None


def requires_confirmation(rules: list[dict], party_size: int | None) -> bool:
    """Deterministic check: does any active rule auto-approve this booking?"""
    if party_size is None:
        return True
    for r in rules:
        n = auto_book_max_party(r["rule_text"])
        if n is not None and party_size <= n:
            return False
    return True
