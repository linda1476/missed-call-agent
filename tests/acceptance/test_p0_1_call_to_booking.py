"""P0-1: a recorded call goes in, a correct booking JSON comes out.

Fixture: tests/fixtures/call_01_booking.wav — synthesized speech:
    "Hi, I'd like to book a table for two on Friday at 7 PM.
     My name is Dana."
(Regenerate with tests/fixtures/generate_fixtures.py.)
"""

from pathlib import Path

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.voice.pipeline import CallPipeline
from receptionist.voice.stt import WhisperSTT

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "call_01_booking.wav"
STORE = "acceptance-p0-1"


def test_recorded_call_produces_correct_booking(tmp_path):
    assert FIXTURE.exists(), f"missing audio fixture: {FIXTURE}"
    db = tmp_path / "store.db"
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, ["fri-1800", "fri-1900", "fri-2000", "sat-1200"])

    pipe = CallPipeline(store, slots, STORE,
                        extractor=DeterministicExtractor(),
                        stt=WhisperSTT("tiny.en"))
    result = pipe.process_audio(str(FIXTURE), caller_id="+14155550101")

    assert result.replies, "agent produced no replies"
    booking = result.booking
    assert booking is not None, "no booking JSON produced"
    assert booking["status"] == "confirmed"
    assert booking["slot_id"] == "fri-1900"
    assert booking["party_size"] == 2
    assert booking["name"] == "Dana"
