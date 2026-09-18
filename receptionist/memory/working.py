"""Working memory: lives for exactly one call, destroyed after handoff.

Holds the caller id, the current-value slots + rules loaded at call start,
this call's requests / confirmations / unresolved items, and tool results.
The raw transcript exists only here and is never persisted after handoff.
"""

import time
from dataclasses import dataclass, field


@dataclass
class WorkingMemory:
    store_id: str
    call_id: str
    caller_id: str
    created_at: float = field(default_factory=time.time)
    current_slots: dict = field(default_factory=dict)
    rules: list = field(default_factory=list)
    turns: list = field(default_factory=list)          # [{speaker, text}]
    requests: list = field(default_factory=list)
    confirmed: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    open_bookings: list = field(default_factory=list)  # live holds at load

    @property
    def transcript(self) -> str:
        return "\n".join(f"{t['speaker']}: {t['text']}" for t in self.turns)

    def add_turn(self, speaker: str, text: str) -> None:
        self.turns.append({"speaker": speaker, "text": text})

    def discard(self) -> None:
        """Post-handoff teardown: drop the raw transcript and everything
        derived from it verbatim — turns, per-utterance requests, and tool
        results. Only extracted/confirmed state survives."""
        self.turns.clear()
        self.requests.clear()
        self.tool_results.clear()
