"""Regression tests for dialogue-flow correctness and tool/server wiring.

These cover bugs found in review that the acceptance suite did not:
- declining a confirmation prompt must NOT book
- cancelled bookings must not be greeted/cancelled/changed as active
- a failed change request must keep the caller's original booking
- bare answers ("two") must answer "for how many people?"
- report_emit must run the real post-call handoff
- tool endpoints require auth when TOOLS_API_KEY is set
"""

import pytest
from fastapi.testclient import TestClient

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.store import MemoryStore
from receptionist.tools.handlers import ToolContext, dispatch
from receptionist.tools.server import create_app
from receptionist.voice.pipeline import CallPipeline

STORE = "unit"
CALLER = "+14155550001"


def _pipe(db, seed=("fri-1900", "sat-1200", "sat-1900")):
    store = MemoryStore(db)
    slots = SlotTable(db)
    slots.seed(STORE, list(seed))
    return store, slots, CallPipeline(store, slots, STORE,
                                      extractor=DeterministicExtractor())


def _book(db, caller=CALLER, slot="fri-1900"):
    """Helper: run a full booking call."""
    _, _, pipe = _pipe(db)
    wm = pipe.start_call(caller)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM, name is Dana.", "Yes."])
    pipe.end_call(wm)
    assert res.booking["slot_id"] == slot
    return res


# ---- confirmation gate ----

def test_decline_does_not_book(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM, name is Dana.",
        "No thanks, I changed my mind.",
    ])
    assert res.booking is None
    assert slots.get(STORE, "fri-1900")["status"] == "free"


def test_gibberish_at_confirm_reasks_not_books(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "banana hammock",
    ])
    assert res.booking is None
    assert "shall i book" in res.replies[-1].lower()
    assert slots.get(STORE, "fri-1900")["status"] == "free"


def test_correction_at_confirm_reasks_then_books_new_slot(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "Actually make it Saturday at noon.",
        "Yes.",
    ])
    assert "sat at 12:00" in "\n".join(res.replies)
    assert res.booking["slot_id"] == "sat-1200"


def test_name_only_reply_at_confirm_still_books(tmp_path):
    """P0-1 shape: confirming details then giving a name must book."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "I'd like to book a table for two on Friday at 7 PM.",
        "My name is Dana.",
    ])
    assert res.booking is not None
    assert res.booking["slot_id"] == "fri-1900"
    assert res.booking["name"] == "Dana"


# ---- cancelled bookings ----

def test_cancelled_booking_not_greeted_as_active(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm2 = pipe.start_call(CALLER)
    r2 = pipe.run_utterances(wm2, ["I need to cancel my reservation."])
    pipe.end_call(wm2)
    assert slots.get(STORE, "fri-1900")["status"] == "free"

    wm3 = pipe.start_call(CALLER)
    greeting = pipe.greeting(wm3)
    assert "fri at 19:00" not in greeting
    assert "party of 0" not in greeting
    assert "keep it" not in greeting


def test_cancel_reports_booking_cancelled(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm2 = pipe.start_call(CALLER)
    pipe.run_utterances(wm2, ["Cancel my reservation please."])
    report = pipe.end_call(wm2)
    assert report["category"] == "booking_cancelled"
    assert report["booking"]["status"] == "cancelled"


# ---- change requests ----

def test_change_to_taken_slot_keeps_original(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "sat-1200", "+1999", 4)  # someone else holds it

    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Can we move it to Saturday at noon for two?", "Yes."])
    assert "just taken" in "\n".join(res.replies)
    # original booking survived the failed change
    orig = slots.get(STORE, "fri-1900")
    assert orig["status"] == "reserved" and orig["held_by"] == CALLER


def test_change_to_free_slot_swaps(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)

    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Can we move it to Saturday at noon for two?", "Yes."])
    assert res.booking["slot_id"] == "sat-1200"
    assert res.booking.get("moved_from") == "fri-1900"
    assert slots.get(STORE, "fri-1900")["status"] == "free"
    assert slots.get(STORE, "sat-1200")["held_by"] == CALLER


def test_change_to_own_slot_is_noop(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, ["Move it to Friday at 7 PM."])
    assert "already your booking" in res.replies[-1]
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER


# ---- bare answers ----

def test_bare_number_answers_party_question(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table on Friday at 7 PM.",   # party missing -> agent asks
        "two",                        # bare answer
        "Yes.",
    ])
    assert "for how many" in res.replies[1].lower()
    assert res.booking["party_size"] == 2


def test_keep_it_affirms_existing_booking(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, ["Keep it, thanks."])
    assert "all set" in res.replies[-1]
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER


def test_tomorrow_resolves_to_weekday_slot(tmp_path):
    from datetime import date, timedelta
    from receptionist.memory.extract import fields_to_slot_id
    tomorrow = date.today() + timedelta(days=1)
    www = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][tomorrow.weekday()]
    assert fields_to_slot_id({"day_offset": 1, "time": "1900"}) == f"{www}-1900"
    # stale weekday must not win over an explicit "tomorrow"
    assert fields_to_slot_id({"weekday": "fri", "day_offset": 1,
                              "time": "1900"}) == f"{www}-1900"


def test_tomorrow_instead_at_confirm_reasks_not_books_old(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "Tomorrow instead.",
    ])
    # must not silently book the original Friday slot
    assert res.booking is None or res.booking["slot_id"] != "fri-1900"


# ---- tool dispatch: WM lifecycle + validation ----

def test_report_emit_runs_real_handoff(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)

    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    assert loaded["call_id"].startswith("call_")
    dispatch(ctx, "booking_reserve",
             {"slot_id": "fri-1900", "party_size": 2, "caller_id": CALLER})
    out = dispatch(ctx, "report_emit", {"call_id": loaded["call_id"]})

    assert out["ok"] and out["report"]["category"] == "booking_confirmed"
    assert loaded["call_id"] not in ctx.active_calls
    # handoff really ran: summary + owner report in history
    kinds = {r["kind"] for r in store.recent_by_kind(STORE, "call_summary")}
    assert "call_summary" in kinds
    assert store.recent_by_kind(STORE, "owner_report")
    # second emit on the same call id fails
    again = dispatch(ctx, "report_emit", {"call_id": loaded["call_id"]})
    assert again["ok"] is False


def test_dispatch_bad_args_return_error_not_500(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    assert dispatch(ctx, "memory_load", {})["ok"] is False
    assert dispatch(ctx, "booking_reserve",
                    {"slot_id": "fri-1900", "party_size": "abc",
                     "caller_id": CALLER})["ok"] is False
    assert dispatch(ctx, "booking_reserve",
                    {"slot_id": "fri-1900", "party_size": 99,
                     "caller_id": CALLER})["ok"] is False
    assert dispatch(ctx, "nope", {})["ok"] is False


# ---- HTTP server: auth + endpoints ----

def test_server_requires_key_when_set(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret")
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.post("/tools/memory_load", json={"caller_id": CALLER}
                  ).status_code == 401
    assert c.get("/dashboard").status_code == 401
    assert c.post("/dashboard/correct", json={"rule_text": "x"}
                  ).status_code == 401
    ok = c.post("/tools/memory_load", json={"caller_id": CALLER},
                headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200 and "call_id" in ok.json()
    assert c.get("/dashboard?key=sekret").status_code == 200


def test_call_session_endpoints(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret")
    c = TestClient(app)
    # anonymous by design (P0-8 test sends no auth headers)
    sid = c.get("/call/start").json()["session_id"]
    st = c.get(f"/call/{sid}/status").json()
    assert st["alive"] is True
    assert c.post(f"/call/{sid}/end").json()["ok"] is True
    assert c.get("/call/sess_missing/status").status_code == 404


def test_spend_cap_blocks_new_sessions(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", spend_cap_usd=1.0)
    c = TestClient(app)
    assert c.get("/call/start").status_code == 200
    # simulate the day's usage reaching the $1 cap (800s at $4.50/hr)
    app.state.spend.charge(900)
    r = c.get("/call/start")
    assert r.status_code == 429 and r.json()["ok"] is False
