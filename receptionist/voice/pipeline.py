"""Call pipeline: wires STT -> responder -> CAS booking -> handoff.

`process_audio` is the acceptance-test entry for P0-1: a recorded audio file
goes in, a booking JSON comes out. `run_utterances` does the same with text
(scenario replays, cross-call memory tests). Both end in run_handoff.
"""

import re
import uuid
from dataclasses import dataclass, field

from ..booking.slots import SlotTable
from ..memory.extract import DeterministicExtractor, Extractor
from ..memory.handoff import run_handoff
from ..memory.store import MemoryStore
from ..memory.working import WorkingMemory
from ..report.emit import build_report
from .respond import Responder
from .stt import WhisperSTT

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class CallResult:
    call_id: str
    caller_id: str
    replies: list = field(default_factory=list)
    booking: dict | None = None
    report: dict = field(default_factory=dict)


class CallPipeline:
    def __init__(self, store: MemoryStore, slots: SlotTable, store_id: str,
                 extractor: Extractor | None = None, stt=None):
        self.store = store
        self.slots = slots
        self.store_id = store_id
        self.extractor = extractor or DeterministicExtractor()
        self.stt = stt or WhisperSTT()

    # ---- call lifecycle (memory.load equivalent) ----

    def start_call(self, caller_id: str, call_id: str | None = None) -> WorkingMemory:
        wm = WorkingMemory(
            store_id=self.store_id,
            call_id=call_id or f"call_{uuid.uuid4().hex[:12]}",
            caller_id=caller_id,
        )
        wm.current_slots = self.store.get_current(self.store_id, caller_id)
        wm.rules = self.store.get_rules(self.store_id)
        wm.requests = []
        return wm

    def greeting(self, wm: WorkingMemory) -> str:
        return Responder(self.store_id, self.slots).greeting(wm)

    # ---- turn handling ----

    def run_utterances(self, wm: WorkingMemory, utterances: list[str]) -> CallResult:
        responder = Responder(self.store_id, self.slots)
        result = CallResult(call_id=wm.call_id, caller_id=wm.caller_id)
        greet = responder.greeting(wm)
        wm.add_turn("agent", greet)
        result.replies.append(greet)
        for text in utterances:
            wm.add_turn("user", text)
            wm.requests.append(text)
            reply = responder.handle_utterance(wm, text)
            wm.add_turn("agent", reply)
            result.replies.append(reply)
        result.booking = wm.confirmed.get("booking")
        return result

    def process_audio(self, audio_path: str, caller_id: str,
                      call_id: str | None = None) -> CallResult:
        """P0-1 path: recorded file in -> booking JSON out."""
        wm = self.start_call(caller_id, call_id)
        transcript = self.stt.transcribe(audio_path)
        utterances = [s for s in _SENT_SPLIT.split(transcript) if s.strip()]
        result = self.run_utterances(wm, utterances)
        result.report = self.end_call(wm)
        return result

    def end_call(self, wm: WorkingMemory) -> dict:
        return run_handoff(self.store, wm, self.extractor,
                           report_builder=build_report)
