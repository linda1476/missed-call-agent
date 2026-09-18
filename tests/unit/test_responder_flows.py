"""Regression tests for dialogue-flow correctness and tool/server wiring.

These cover bugs found in review that the acceptance suite did not:
- declining a confirmation prompt must NOT book
- cancelled bookings must not be greeted/cancelled/changed as active
- a failed change request must keep the caller's original booking
- bare answers ("two") must answer "for how many people?"
- report_emit must run the real post-call handoff
- tool endpoints require auth when TOOLS_API_KEY is set
"""

import time

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


# ---- split-turn change: intent must survive across utterances ----

def test_split_turn_change_swaps_not_double_books(tmp_path):
    """"Can we move it?" -> "Saturday at noon" must swap, not double-book."""
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Can we move it?",
        "Saturday at noon.",
        "Yes.",
    ])
    pipe.end_call(wm)
    assert res.booking["slot_id"] == "sat-1200"
    assert res.booking.get("moved_from") == "fri-1900"
    assert slots.get(STORE, "sat-1200")["held_by"] == CALLER
    # the original was released — exactly one active booking
    assert slots.get(STORE, "fri-1900")["status"] == "free"


def test_abandoned_change_does_not_release_original(tmp_path):
    """Dropping a pending change must not free the existing booking when a
    later unrelated reservation is made."""
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Can we move it to Saturday at noon?",   # confirm prompt, intent set
        "Cancel that.",                           # drop the pending change
        "Table for two on Saturday at 7 PM.",     # unrelated NEW request
        "Yes.",
    ])
    pipe.end_call(wm)
    assert res.booking["slot_id"] == "sat-1900"
    assert "moved_from" not in res.booking
    # the original booking survives — caller now holds two by intent
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER
    assert slots.get(STORE, "sat-1900")["held_by"] == CALLER


def test_declined_change_keeps_original_and_clears_intent(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Can we move it to Saturday at noon?",
        "No.",                                    # decline the change
        "Actually keep it.",
    ])
    pipe.end_call(wm)
    assert res.booking is None
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER
    assert slots.get(STORE, "sat-1200")["status"] == "free"


# ---- name extraction: no false positives ----

def test_non_name_phrases_do_not_store_names(tmp_path):
    from receptionist.memory.extract import parse_booking_fields
    for phrase in ("it's fine", "this is ridiculous",
                   "it's too expensive", "i'm calling about a table"):
        assert "name" not in parse_booking_fields(phrase), phrase
    # real introductions still work
    assert parse_booking_fields("it's Dana")["name"] == "Dana"
    assert parse_booking_fields("i'm Rosa")["name"] == "Rosa"
    assert parse_booking_fields("my name is dana")["name"] == "Dana"


def test_its_fine_at_confirm_books_without_fake_name(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "It's fine, book it.",
    ])
    assert res.booking is not None
    assert res.booking.get("name") in (None, "Dana")  # never "Fine"


# ---- confirm gate: unparsed party corrections must not be ignored ----

def test_party_correction_with_name_at_confirm_reasks(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "Name is Dana, we are actually five.",   # name + unparsed number
        "Yes.",
    ])
    assert res.booking["party_size"] == 5        # not the stale 2
    assert res.booking["name"] == "Dana"
    assert any("for 5" in r for r in res.replies)  # re-asked with new party


def test_trailing_for_two_parses_party(tmp_path):
    from receptionist.memory.extract import parse_booking_fields
    assert parse_booking_fields(
        "Book me Saturday at noon for two")["party_size"] == 2


# ---- booking_release tool ----

def test_booking_release_tool_cancels_and_reports(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)

    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    dispatch(ctx, "booking_reserve",
             {"slot_id": "fri-1900", "party_size": 2, "caller_id": CALLER})
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER

    out = dispatch(ctx, "booking_release",
                   {"slot_id": "fri-1900", "caller_id": CALLER})
    assert out["ok"] is True
    assert slots.get(STORE, "fri-1900")["status"] == "free"

    # non-holder cannot release
    slots.reserve(STORE, "sat-1200", "+1999", 4)
    bad = dispatch(ctx, "booking_release",
                   {"slot_id": "sat-1200", "caller_id": CALLER})
    assert bad["ok"] is False
    assert slots.get(STORE, "sat-1200")["held_by"] == "+1999"

    rep = dispatch(ctx, "report_emit", {"call_id": loaded["call_id"]})
    assert rep["report"]["category"] == "booking_cancelled"


def test_booking_release_in_session_tool_list():
    from receptionist.voice.session import load_tool_schemas
    names = {t["name"] for t in load_tool_schemas()}
    assert {"memory_load", "memory_search", "booking_reserve",
            "booking_release", "report_emit"} <= names


# ---- real call sessions over HTTP ----

def test_call_session_is_a_real_call(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", seed_slots=["fri-1900", "sat-1200"])
    c = TestClient(app)
    start = c.post("/call/start").json()
    sid = start["session_id"]
    assert start["greeting"]

    r = c.post(f"/call/{sid}/turn",
               json={"text": "Table for two on Friday at 7 PM, name is Dana."})
    assert r.json()["ok"] and "shall i book" in r.json()["reply"].lower()
    r = c.post(f"/call/{sid}/turn", json={"text": "Yes."})
    assert "all set" in r.json()["reply"].lower()

    end = c.post(f"/call/{sid}/end").json()
    assert end["ok"] and end["report"]["category"] == "booking_confirmed"
    assert end["report"]["booking"]["slot_id"] == "fri-1900"

    # ended sessions are gone; unknown turns 404
    assert c.post(f"/call/{sid}/turn", json={"text": "hi"}
                  ).status_code == 404
    # the booking really persisted to the shared slot table
    store = MemoryStore(tmp_path / "s.db")
    assert store.recent_by_kind(STORE, "owner_report")


def test_session_cap_returns_429(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret")
    c = TestClient(app)
    for _ in range(100):
        assert c.post("/call/start").status_code == 200
    assert c.post("/call/start").status_code == 429


# ---- moves through the tool path (reserve new -> release old) ----

def test_tool_move_keeps_new_booking_and_reports_confirmed(tmp_path):
    """reserve(new) -> release(old) is a MOVE, not a cancellation: the
    report must show the new confirmed booking and memory must keep it."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    slots.reserve(STORE, "fri-1900", CALLER, 2)  # caller's existing booking

    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    dispatch(ctx, "booking_reserve",
             {"slot_id": "sat-1200", "party_size": 2, "caller_id": CALLER})
    out = dispatch(ctx, "booking_release",
                   {"slot_id": "fri-1900", "caller_id": CALLER})
    assert out["ok"] is True
    assert slots.get(STORE, "fri-1900")["status"] == "free"

    rep = dispatch(ctx, "report_emit", {"call_id": loaded["call_id"]})
    assert rep["report"]["category"] == "booking_confirmed"
    assert rep["report"]["booking"]["slot_id"] == "sat-1200"

    # next call remembers the NEW booking — not a cancelled fri-1900
    loaded2 = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    assert loaded2["current_slots"]["last_booking_slot"] == "sat-1200"
    assert loaded2["current_slots"]["last_booking_status"] == "confirmed"


def test_tool_release_same_slot_still_cancels(tmp_path):
    """Releasing the caller's only confirmed booking is a real cancel."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    dispatch(ctx, "booking_reserve",
             {"slot_id": "fri-1900", "party_size": 2, "caller_id": CALLER})
    out = dispatch(ctx, "booking_release",
                   {"slot_id": "fri-1900", "caller_id": CALLER})
    assert out["ok"] is True
    rep = dispatch(ctx, "report_emit", {"call_id": loaded["call_id"]})
    assert rep["report"]["category"] == "booking_cancelled"
    assert rep["report"]["booking"]["status"] == "cancelled"


# ---- anonymous sessions can't impersonate a caller ----

def test_call_start_caller_id_requires_api_key(tmp_path):
    """?caller_id= on /call/start is honored only with the API key — an
    anonymous session must never see or touch another caller's memory."""
    db = tmp_path / "s.db"
    _book(db, caller="+14155550100")   # known caller: name Dana, fri-1900
    app = create_app(store_path=str(db), store_id=STORE, api_key="sekret")
    c = TestClient(app)

    anon = c.post("/call/start",
                  params={"caller_id": "+14155550100"}).json()
    assert "Dana" not in anon["greeting"]
    assert "19:00" not in anon["greeting"]
    # the anon session can't cancel the victim's booking either
    r = c.post(f"/call/{anon['session_id']}/turn",
               json={"text": "Cancel my reservation."})
    assert "couldn't find" in r.json()["reply"].lower()
    assert SlotTable(db).get(STORE, "fri-1900")["held_by"] == "+14155550100"

    keyed = c.post("/call/start", params={"caller_id": "+14155550100"},
                   headers={"Authorization": "Bearer sekret"}).json()
    assert "Dana" in keyed["greeting"]
    assert "19:00" in keyed["greeting"]


# ---- session lifecycle ----

def test_turn_slides_session_idle_ttl(tmp_path):
    """Active conversations aren't cut at the TTL — /turn extends it."""
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", seed_slots=["fri-1900"])
    c = TestClient(app)
    sid = c.post("/call/start").json()["session_id"]
    before = app.state.sessions[sid]["ends_at"]
    time.sleep(0.05)
    assert c.post(f"/call/{sid}/turn", json={"text": "hi"}).json()["ok"]
    assert app.state.sessions[sid]["ends_at"] > before


# ---- negated cancel: "don't cancel" keeps the booking ----

def test_dont_cancel_keeps_booking(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "I don't want to cancel it, I was just checking."])
    pipe.end_call(wm)
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER
    assert "cancelled" not in res.replies[-1].lower()


# ---- word-number times: "at seven" is a time, not a party ----

def test_word_number_time_parses():
    from receptionist.memory.extract import parse_booking_fields
    f = parse_booking_fields("A table at seven on Friday for two")
    assert f["time"] == "1900"
    assert f["party_size"] == 2


def test_word_time_correction_at_confirm_reasks(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "Actually at six.",   # word-time correction, not "party of six"
    ])
    assert "18:00" in res.replies[-1]
    assert res.booking is None


# ---- stale day_offset must not shadow an explicit weekday correction ----

def test_tomorrow_then_friday_correction_books_friday(tmp_path):
    """'tomorrow at 7' -> 'actually Friday at 7' must re-ask and book Friday —
    the stale day_offset must not silently book tomorrow's slot."""
    from datetime import date, timedelta
    tom = date.today() + timedelta(days=1)
    www = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][tom.weekday()]
    tom_slot = f"{www}-1900"
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db, seed=("fri-1900", tom_slot, "sat-1200"))
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two tomorrow at 7 PM.",
        "Actually make it Friday at 7 PM.",
        "Yes.",
    ])
    # replies[0] is the greeting; replies[2] is the re-ask after correction
    assert "fri at 19:00" in res.replies[2]
    assert res.booking["slot_id"] == "fri-1900"
    assert slots.get(STORE, tom_slot)["status"] == "free"


def test_unresolvable_date_reask_drops_stale_slot(tmp_path):
    """'actually September 25' can't map to a slot — the agent asks for the
    day again instead of re-offering the stale one."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.",
        "Actually make it September 25.",
    ])
    assert "what day" in res.replies[-1].lower()
    assert res.booking is None
    assert slots.get(STORE, "fri-1900")["status"] == "free"


# ---- same-call cancel / change / keep on a just-made booking ----

def test_cancel_same_call_booking_releases_slot(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.", "Yes.",
        "Actually, cancel that.",
    ])
    assert "cancelled" in res.replies[-1]
    assert wm.confirmed["booking"]["status"] == "cancelled"
    assert slots.get(STORE, "fri-1900")["status"] == "free"
    report = pipe.end_call(wm)
    assert report["category"] == "booking_cancelled"


def test_change_same_call_booking_moves_not_double_books(tmp_path):
    """'move it to Saturday' after booking Friday this call must swap, not
    stack a second reservation."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM, name is Dana.", "Yes.",
        "Can we move it to Saturday at noon?",
        "Yes.",
    ])
    pipe.end_call(wm)
    assert res.booking["slot_id"] == "sat-1200"
    assert res.booking.get("moved_from") == "fri-1900"
    assert res.booking["party_size"] == 2   # carried over, not re-asked
    assert res.booking["name"] == "Dana"     # name survives the move
    assert slots.get(STORE, "fri-1900")["status"] == "free"
    assert slots.get(STORE, "sat-1200")["held_by"] == CALLER


def test_keep_same_call_booking(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, [
        "Table for two on Friday at 7 PM.", "Yes.", "Keep it, thanks."])
    assert "all set" in res.replies[-1]
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER


def test_remove_phrasing_cancels(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, ["Please remove my reservation."])
    assert slots.get(STORE, "fri-1900")["status"] == "free"
    assert "cancelled" in res.replies[-1]


def test_dont_move_keeps_booking(tmp_path):
    db = tmp_path / "s.db"
    _book(db)
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    res = pipe.run_utterances(wm, ["I don't want to move it, Friday is fine."])
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER
    # "Friday is fine" restates the active day — affirmed, not a new request
    assert "all set" in res.replies[-1]


# ---- spam calls are categorized as spam ----

def test_spam_pitch_categorizes_spam(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.run_utterances(wm, [
        "Hi, this is about your car's extended warranty — press 1 now."])
    report = pipe.end_call(wm)
    assert report["category"] == "spam"


def test_legit_call_not_flagged_spam(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.run_utterances(wm, [
        "Do you open on Sundays? I want to book soon."])
    report = pipe.end_call(wm)
    assert report["category"] != "spam"


# ---- name memory persists beyond bookings ----

def test_inquiry_caller_name_persists(tmp_path):
    """A caller who gives a name without booking is still remembered."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.run_utterances(wm, ["My name is Dana, do you open on Sundays?"])
    pipe.end_call(wm)
    assert store.get_current(STORE, CALLER)["name"] == "Dana"


# ---- sweep hands off abandoned calls instead of dropping them ----

def test_swept_call_still_hands_off(tmp_path):
    """A stale call the agent never closed via report_emit still runs the
    handoff on sweep — summary, facts, report are not silently lost."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    dispatch(ctx, "booking_reserve",
             {"slot_id": "fri-1900", "party_size": 2, "caller_id": CALLER})
    ctx.active_calls[loaded["call_id"]].created_at = time.time() - 7200
    dispatch(ctx, "memory_load", {"caller_id": "+1999"})   # triggers sweep
    assert loaded["call_id"] not in ctx.active_calls
    assert store.recent_by_kind(STORE, "call_summary")
    assert store.recent_by_kind(STORE, "owner_report")
    assert store.get_current(STORE, CALLER)["last_booking_slot"] == "fri-1900"


# ---- concurrent /end callers share one handoff ----

def test_concurrent_end_returns_report_not_none(tmp_path):
    import threading
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", seed_slots=["fri-1900"])
    c = TestClient(app)
    sid = c.post("/call/start").json()["session_id"]
    c.post(f"/call/{sid}/turn",
           json={"text": "Table for two on Friday at 7 PM."})
    c.post(f"/call/{sid}/turn", json={"text": "Yes."})
    out = {}

    def end(i):
        out[i] = c.post(f"/call/{sid}/end")

    ts = [threading.Thread(target=end, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in out.values() if r.status_code == 200]
    assert oks, "no successful /end"
    for r in oks:
        assert r.json()["ok"] and r.json()["report"] is not None
        assert r.json()["report"]["category"] == "booking_confirmed"
