"""P0-6: an owner correction becomes a procedural rule (with source); the
same scenario that previously required confirmation is then handled with no
owner confirmation step."""

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.voice.pipeline import CallPipeline

STORE = "acceptance-p0-6"
UTTERANCES = ["Table for two on Friday at 7 PM, name is Dana."]
RULE = "auto-book parties of 4 or fewer without confirming"


def _run_call(db, caller):
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, ["fri-1900"])
    pipe = CallPipeline(store, slots, STORE, extractor=DeterministicExtractor())
    wm = pipe.start_call(caller)
    res = pipe.run_utterances(wm, UTTERANCES)
    pipe.end_call(wm)
    return res


def test_correction_turns_into_rule_and_skips_confirm(tmp_path):
    # Baseline: no rule -> agent asks for confirmation before booking.
    base = _run_call(tmp_path / "base.db", "+14155550106")
    assert any("shall i book" in r.lower() for r in base.replies), \
        "baseline should ask for confirmation"
    assert base.booking is None  # nothing booked yet — still unconfirmed

    # Owner corrects via dashboard path: rule stored WITH its source.
    store = MemoryStore(tmp_path / "ruled.db")
    rule_id = store.owner_correct(
        STORE, RULE, source_text=f"owner typed: '{RULE}'")
    rules = store.get_rules(STORE)
    assert rules and rules[0]["id"] == rule_id
    assert rules[0]["source_text"], "rule must carry its correction source"
    assert rules[0]["source_at"]

    # Same scenario under the rule: booked immediately, no confirm prompt.
    db2 = tmp_path / "ruled.db"
    store2 = MemoryStore(db2)
    slots = SlotTable(db2)
    slots.seed(STORE, ["fri-1900"])
    # rule lives in the same store the pipeline reads
    assert store2.get_rules(STORE), "rule not visible to pipeline store"
    pipe = CallPipeline(store2, slots, STORE, extractor=DeterministicExtractor())
    wm = pipe.start_call("+14155550107")
    res = pipe.run_utterances(wm, UTTERANCES)
    pipe.end_call(wm)

    assert res.booking and res.booking["status"] == "confirmed"
    assert res.booking["slot_id"] == "fri-1900"
    assert not any("shall i book" in r.lower() for r in res.replies), \
        "confirmation prompt appeared despite owner rule"
