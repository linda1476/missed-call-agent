"""P0-3: after a call ends, the three memory kinds are written separately —
summary to history, facts to current slots, booking state to the slot table —
and the raw transcript is not persisted anywhere."""

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.voice.pipeline import CallPipeline

STORE = "acceptance-p0-3"
CALLER = "+14155550103"
TOKEN = "ZQXWVTRAWTOKEN"  # unique marker said 'on the call' only


def test_handoff_separates_three_kinds_and_purges_transcript(tmp_path):
    db = tmp_path / "store.db"
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, ["fri-1900"])
    pipe = CallPipeline(store, slots, STORE,
                        extractor=DeterministicExtractor())

    wm = pipe.start_call(CALLER)
    pipe.run_utterances(wm, [
        f"{TOKEN} I'd like a table for four on Friday at 7 PM.",
        "Yes.",
    ])
    # The marker definitely existed in working memory.
    assert TOKEN in wm.transcript
    pipe.end_call(wm)

    # 1) summary -> history (append-only kind 'call_summary')
    summaries = store.recent_by_kind(STORE, "call_summary")
    assert summaries and "fri-1900" in summaries[0]["text"]

    # 2) facts -> current slots (overwrite store)
    cur = store.get_current(STORE, CALLER)
    assert cur["last_booking_slot"] == "fri-1900"
    assert cur["last_booking_party_size"] == "4"
    assert cur["last_booking_status"] == "confirmed"

    # 3) state -> slot table reflects the confirmed reservation
    slot = slots.get(STORE, "fri-1900")
    assert slot["status"] == "reserved" and slot["held_by"] == CALLER

    # raw transcript: nowhere in any long-term store
    assert TOKEN not in store.dump_all_text(STORE)
    # working memory was discarded
    assert wm.transcript == ""
