"""P0-2: on the second call, the caller's first call is loaded by caller id
and reflected in the agent's response."""

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.tools.handlers import ToolContext, dispatch
from receptionist.voice.pipeline import CallPipeline

STORE = "acceptance-p0-2"
CALLER = "+14155550102"


def test_second_call_recalls_first(tmp_path):
    db = tmp_path / "store.db"
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, ["fri-1900", "sat-1200"])
    pipe = CallPipeline(store, slots, STORE,
                        extractor=DeterministicExtractor())

    # Call 1: new caller books.
    wm1 = pipe.start_call(CALLER)
    r1 = pipe.run_utterances(wm1, [
        "Hi, I'd like to book a table for two on Friday at 7 PM. My name is Dana.",
        "Yes, please.",
    ])
    pipe.end_call(wm1)
    assert r1.booking["slot_id"] == "fri-1900"

    # Call 2: same caller id -> memory.load returns first call's outcome...
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    assert loaded["current_slots"]["last_booking_slot"] == "fri-1900"
    assert loaded["current_slots"]["last_booking_party_size"] == "2"

    # ...and the greeting reflects it before the caller says anything.
    wm2 = pipe.start_call(CALLER)
    greeting = pipe.greeting(wm2)
    assert "19:00" in greeting or "fri" in greeting.lower()
    assert "party of 2" in greeting
    assert "Dana" in greeting

    # history.search also surfaces the first call's summary.
    hits = dispatch(ctx, "memory_search",
                    {"caller_id": CALLER, "query": "booking friday", "k": 3})
    assert any("fri-1900" in h["text"] or "fri-1900" in (h["meta"] or "")
               for h in hits["results"])
