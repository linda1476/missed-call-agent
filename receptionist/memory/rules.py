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
_PARTY_LTE = re.compile(
    r"part(?:y|ies)\s+of\s+(\d+)\s+or\s+(?:less|fewer|under)|"
    r"(?:under|up to|at most|<=?)\s*(\d+)\s*(?:people|guests|persons)?", re.I)


def auto_book_max_party(rule_text: str) -> int | None:
    """If the rule says 'auto-book parties of N or less', return N."""
    if not _AUTO_BOOK.search(rule_text):
        return None
    m = _PARTY_LTE.search(rule_text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def requires_confirmation(rules: list[dict], party_size: int | None) -> bool:
    """Deterministic check: does any active rule auto-approve this booking?"""
    if party_size is None:
        return True
    for r in rules:
        n = auto_book_max_party(r["rule_text"])
        if n is not None and party_size <= n:
            return False
    return True
