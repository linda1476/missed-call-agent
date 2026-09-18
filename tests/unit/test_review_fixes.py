"""Regression tests for the review-fix pass — protocol, responder,
booking-cap, guard, handoff, and concurrency behavior that the acceptance
suite does not cover.

- session.py must speak the documented Voice Agent API protocol
  (fake-WebSocket tests: session.ready gating, call_id echo, JSON-string
  tool.result drained on reply.done, session.end on unclean teardown)
- offered alternatives must be acceptable (pick, ordinal, lone-offer yes)
- a caller may hold several bookings; the slot table stays authoritative
- per-caller booking cap; a move is not blocked by the cap (swap_from)
- the reply guard regenerates replies that contradict live state
- 'under N' owner rules mean N-1, not N
- handoff records every booking action and purges raw request text
"""

import asyncio
import json
import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

from receptionist.booking.slots import SlotTable
from receptionist.memory.extract import DeterministicExtractor
from receptionist.memory.rules import auto_book_max_party
from receptionist.memory.store import MemoryStore
from receptionist.tools.handlers import ToolContext, dispatch
from receptionist.tools.server import create_app
from receptionist.voice.guard import check_reply, enforce_reply, make_reply_guard
from receptionist.voice.pipeline import CallPipeline
from receptionist.voice.session import run_session

STORE = "unit"
CALLER = "+14155550001"


def _pipe(db, seed=("fri-1800", "fri-1900", "sat-1200", "sat-1900"),
          cap=None):
    store = MemoryStore(db)
    slots = SlotTable(db, per_caller_cap=cap)
    slots.seed(STORE, list(seed))
    return store, slots, CallPipeline(store, slots, STORE,
                                      extractor=DeterministicExtractor())


# ---------------------------------------------------------------- session --
# Fake WebSocket standing in for the Voice Agent API endpoint.

class _FakeWS:
    """Scriptable fake: `incoming` is a list of protocol events (dicts);
    every sent frame is recorded in .sent as parsed JSON."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def _gen():
            for m in self.incoming:
                # Yield between frames — a real socket gives other tasks
                # (the audio sender) loop time; without this the script
                # outruns scheduling.
                await asyncio.sleep(0)
                yield json.dumps(m)
        return _gen()


def _fake_ws_module(ws, record):
    mod = types.ModuleType("websockets")

    def connect(url, additional_headers=None, **kw):
        record["url"] = url
        record["headers"] = additional_headers or {}
        return ws

    mod.connect = connect
    return mod


async def _audio(chunks=(b"\x00\x01", b"\x02\x03")):
    for c in chunks:
        yield c


def _install_fake_ws(monkeypatch, ws, record):
    monkeypatch.setitem(sys.modules, "websockets",
                        _fake_ws_module(ws, record))


def test_session_protocol_happy_path(monkeypatch):
    """ready-gated audio, tool.call -> queued tool.result drained on
    reply.done with call_id + JSON-string result; clean session.ended."""
    ws = _FakeWS([
        {"type": "session.ready"},
        {"type": "tool.call", "call_id": "c-1", "name": "memory_load",
         "arguments": {"caller_id": CALLER}},
        {"type": "reply.done"},
        {"type": "session.ended"},
    ])
    rec = {}
    _install_fake_ws(monkeypatch, ws, rec)
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return {"ok": True, "call_id": "call_1"}

    asyncio.run(run_session(_audio(), dispatch, api_key="k"))

    assert rec["url"] == "wss://agents.assemblyai.com/v1/ws"
    assert rec["headers"]["Authorization"] == "Bearer k"
    types_sent = [m["type"] for m in ws.sent]
    assert types_sent[0] == "session.update"
    # audio streamed only after ready, and it did stream
    assert "input.audio" in types_sent
    assert calls == [("memory_load", {"caller_id": CALLER})]
    tr = next(m for m in ws.sent if m["type"] == "tool.result")
    assert tr["call_id"] == "c-1"
    assert tr["is_error"] is False
    assert json.loads(tr["result"]) == {"ok": True, "call_id": "call_1"}
    # tool.result was sent only after reply.done
    assert types_sent.index("tool.result") > types_sent.index("session.update")
    # clean end: no session.end needed after session.ended
    assert "session.end" not in types_sent


def test_session_tool_result_is_error_flag(monkeypatch):
    ws = _FakeWS([
        {"type": "session.ready"},
        {"type": "tool.call", "call_id": "c-9", "name": "booking_reserve",
         "arguments": {}},
        {"type": "reply.done"},
        {"type": "session.ended"},
    ])
    _install_fake_ws(monkeypatch, ws, {})
    asyncio.run(run_session(_audio(()), lambda n, a: {"ok": False},
                            api_key="k"))
    tr = next(m for m in ws.sent if m["type"] == "tool.result")
    assert tr["is_error"] is True
    assert tr["call_id"] == "c-9"


def test_session_audio_waits_for_ready(monkeypatch):
    """No input.audio may be sent before session.ready — the source waits."""
    ws = _FakeWS([
        {"type": "session.ended"},   # never sends session.ready
    ])
    _install_fake_ws(monkeypatch, ws, {})
    asyncio.run(run_session(_audio(), lambda n, a: {}, api_key="k"))
    assert not any(m["type"] == "input.audio" for m in ws.sent)


def test_session_end_sent_on_unclean_teardown(monkeypatch):
    """Stream ending without session.ended -> session.end goes out so the
    30s billable grace window is not left running."""
    ws = _FakeWS([{"type": "session.ready"}])  # iterator just stops
    _install_fake_ws(monkeypatch, ws, {})
    asyncio.run(run_session(_audio(()), lambda n, a: {}, api_key="k"))
    assert any(m["type"] == "session.end" for m in ws.sent)


def test_session_error_raises_and_still_ends(monkeypatch):
    ws = _FakeWS([
        {"type": "session.ready"},
        {"type": "session.error", "code": "bad_request",
         "message": "nope"},
    ])
    _install_fake_ws(monkeypatch, ws, {})
    with pytest.raises(RuntimeError, match="bad_request"):
        asyncio.run(run_session(_audio(()), lambda n, a: {}, api_key="k"))
    assert any(m["type"] == "session.end" for m in ws.sent)


def test_session_reply_guard_sends_reply_create(monkeypatch, tmp_path):
    """A transcript that contradicts live state triggers a reply.create
    correction carrying the deterministic fix."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    wm = CallPipeline(store, slots, STORE).start_call(CALLER)
    ws = _FakeWS([
        {"type": "session.ready"},
        {"type": "transcript.agent",
         "text": "You're all set — fri at 19:00 for 2. See you then!"},
        {"type": "session.ended"},
    ])
    _install_fake_ws(monkeypatch, ws, {})
    guard = make_reply_guard(wm, slots, STORE)
    asyncio.run(run_session(_audio(()), lambda n, a: {}, api_key="k",
                            reply_guard=guard))
    rc = next((m for m in ws.sent if m["type"] == "reply.create"), None)
    assert rc is not None
    assert "wasn't able to confirm" in rc["instructions"]


def test_session_interrupted_reply_discards_tool_results(monkeypatch):
    """Docs: on reply.done status='interrupted' the pending tool.result
    accumulators are discarded, not sent."""
    ws = _FakeWS([
        {"type": "session.ready"},
        {"type": "tool.call", "call_id": "c-1", "name": "memory_load",
         "arguments": {"caller_id": CALLER}},
        {"type": "reply.done", "status": "interrupted"},
        {"type": "session.ended"},
    ])
    _install_fake_ws(monkeypatch, ws, {})
    asyncio.run(run_session(_audio(()), lambda n, a: {"ok": True},
                            api_key="k"))
    assert not any(m["type"] == "tool.result" for m in ws.sent)


def test_session_requires_api_key():
    with pytest.raises(RuntimeError):
        asyncio.run(run_session(_audio(()), lambda n, a: {},
                                api_key=None))


# ------------------------------------------------- alternative offers -----

def test_lone_offer_bare_yes_books(tmp_path):
    """Losing a race then saying 'yes' to the single offered alternative
    books it — the flagship concurrency scenario is not a dead end."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db, seed=("fri-1900", "sat-1900"))
    slots.reserve(STORE, "fri-1900", "+other", 2)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM.")
    reply = pipe.turn(wm, "Yes.")
    assert "sat at 19:00" in reply
    reply = pipe.turn(wm, "Yes, that works.")
    assert "all set" in reply.lower()
    row = slots.get(STORE, "sat-1900")
    assert row["status"] == "reserved" and row["held_by"] == CALLER


def test_offer_pick_by_time_books_and_clears_failed_marker(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", "+other", 2)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM.")
    pipe.turn(wm, "Yes.")                       # -> offers alternatives
    pipe.turn(wm, "Saturday at 7 PM works.")    # picks one -> re-confirm
    reply = pipe.turn(wm, "Yes.")
    assert "all set" in reply.lower()
    assert slots.get(STORE, "sat-1900")["held_by"] == CALLER
    # the earlier "slot gone" marker is resolved — not left as a callback
    assert wm.unresolved == []
    rep = pipe.end_call(wm)
    assert rep["category"] == "booking_confirmed"


def test_offer_pick_by_ordinal(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1800", "+o1", 2)
    slots.reserve(STORE, "fri-1900", "+o2", 2)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 6 PM.")
    reply = pipe.turn(wm, "Yes.")
    assert "sat at 12:00" in reply
    pipe.turn(wm, "The second one.")            # ordinal pick
    pipe.turn(wm, "Yes.")
    assert slots.get(STORE, "sat-1900")["held_by"] == CALLER


def test_offer_decline_leaves_unresolved_for_callback(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", "+other", 2)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM.")
    pipe.turn(wm, "Yes.")
    reply = pipe.turn(wm, "No, none of those work.")
    assert slots.get(STORE, "sat-1900")["status"] == "free"
    assert wm.unresolved                         # still a real callback item
    rep = pipe.end_call(wm)
    assert rep["category"] == "booking_failed"


def test_failed_move_offer_still_releases_nothing(tmp_path):
    """A move that loses the new slot keeps the original AND can still
    complete via an offered alternative — releasing the old slot then."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db, seed=("fri-1900", "sat-1200", "sat-1900"))
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    slots.reserve(STORE, "sat-1200", "+other", 2)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Move my booking to Saturday at noon.")
    reply = pipe.turn(wm, "Yes.")   # sat-1200 taken -> offer sat-1900
    assert "sat at 19:00" in reply
    reply = pipe.turn(wm, "Yes.")
    assert "all set" in reply.lower()
    assert slots.get(STORE, "sat-1900")["held_by"] == CALLER
    assert slots.get(STORE, "fri-1900")["status"] == "free"


# ------------------------------------------------------- multi-booking ----

def test_two_bookings_one_call_all_tracked(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM, name is Dana.")
    pipe.turn(wm, "Yes.")
    pipe.turn(wm, "Also a table for four on Saturday at noon.")
    pipe.turn(wm, "Yes.")
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER
    assert slots.get(STORE, "sat-1200")["held_by"] == CALLER
    assert len(wm.confirmed["bookings"]) == 2
    rep = pipe.end_call(wm)
    assert len(rep["bookings"]) == 2
    # history records both booking actions, not just the last
    hist = store.search_history(STORE, CALLER, "booking", k=10)
    assert sum("booking confirmed" in h["text"] for h in hist) == 2


def test_cancel_disambiguates_two_held_slots(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    slots.reserve(STORE, "sat-1200", CALLER, 4)
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "Cancel the Friday one.")
    assert "cancelled fri at 19:00" in reply
    assert slots.get(STORE, "fri-1900")["status"] == "free"
    assert slots.get(STORE, "sat-1200")["held_by"] == CALLER


def test_cancel_ambiguous_asks_which(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    slots.reserve(STORE, "sat-1200", CALLER, 4)
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "I need to cancel.")
    assert "which booking" in reply.lower()
    reply = pipe.turn(wm, "The Saturday one.")
    assert "cancelled sat at 12:00" in reply
    assert slots.get(STORE, "fri-1900")["held_by"] == CALLER


# ------------------------------------------------------------ cap ---------

def test_per_caller_cap_blocks_fifth_booking(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db, cap=2,
                            seed=("a-1", "b-1", "c-1"))
    slots.reserve(STORE, "a-1", CALLER, 2)
    slots.reserve(STORE, "b-1", CALLER, 2)
    res = slots.reserve(STORE, "c-1", CALLER, 2)
    assert res["ok"] is False and res["reason"] == "limit"
    # another caller is unaffected
    assert slots.reserve(STORE, "c-1", "+other", 2)["ok"]


def test_move_at_cap_allowed_via_swap(tmp_path):
    """swap_from excludes the released slot from the cap count — a move at
    the limit is a swap, not a new booking."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db, cap=2,
                            seed=("a-1", "b-1", "c-1"))
    slots.reserve(STORE, "a-1", CALLER, 2)
    slots.reserve(STORE, "b-1", CALLER, 2)
    res = slots.reserve(STORE, "c-1", CALLER, 2, swap_from="a-1")
    assert res["ok"] and res["released_from"] == "a-1"
    assert slots.get(STORE, "a-1")["status"] == "free"
    assert slots.get(STORE, "c-1")["held_by"] == CALLER


def test_failed_swap_keeps_original(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db, cap=2, seed=("a-1", "b-1", "c-1"))
    slots.reserve(STORE, "a-1", CALLER, 2)
    slots.reserve(STORE, "c-1", "+other", 2)
    res = slots.reserve(STORE, "c-1", CALLER, 2, swap_from="a-1")
    assert res["ok"] is False
    assert slots.get(STORE, "a-1")["held_by"] == CALLER  # untouched


def test_tool_swap_from_must_be_callers_slot(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    slots.reserve(STORE, "fri-1900", "+other", 2)
    res = dispatch(ctx, "booking_reserve",
                   {"slot_id": "sat-1200", "party_size": 2,
                    "caller_id": CALLER, "swap_from": "fri-1900"})
    assert res["ok"] is False
    assert slots.get(STORE, "fri-1900")["held_by"] == "+other"


def test_tool_swap_atomic_move(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    loaded = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    res = dispatch(ctx, "booking_reserve",
                   {"slot_id": "sat-1200", "party_size": 2,
                    "caller_id": CALLER, "swap_from": "fri-1900"})
    assert res["ok"] and res["released_from"] == "fri-1900"
    wm = ctx.active_calls[loaded["call_id"]]
    statuses = {b["slot_id"]: b["status"]
                for b in wm.confirmed["bookings"]}
    assert statuses == {"fri-1900": "moved", "sat-1200": "confirmed"}


# ------------------------------------------------------------ guard -------

def test_guard_corrects_false_booking_claim(tmp_path):
    db = tmp_path / "s.db"
    _, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    out = enforce_reply(
        "You're all set — fri at 19:00 for 2. See you then!",
        wm, slots, STORE)
    assert "wasn't able to confirm" in out


def test_guard_passes_true_booking_claim(tmp_path):
    db = tmp_path / "s.db"
    _, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    wm = pipe.start_call(CALLER)
    wm.confirmed["booking"] = {"slot_id": "fri-1900", "party_size": 2,
                               "status": "confirmed"}
    reply = "You're all set — fri at 19:00 for 2. See you then!"
    assert enforce_reply(reply, wm, slots, STORE) == reply


def test_guard_corrects_false_cancel_claim(tmp_path):
    db = tmp_path / "s.db"
    _, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    wm = pipe.start_call(CALLER)
    out = enforce_reply("Done — I've cancelled fri at 19:00.",
                        wm, slots, STORE)
    assert "still on the books" in out


def test_guard_corrects_wrong_party_claim(tmp_path):
    db = tmp_path / "s.db"
    _, slots, pipe = _pipe(db)
    slots.reserve(STORE, "fri-1900", CALLER, 4)
    wm = pipe.start_call(CALLER)
    wm.confirmed["booking"] = {"slot_id": "fri-1900", "party_size": 4,
                               "status": "confirmed"}
    out = enforce_reply("You're all set — fri at 19:00 for 2.",
                        wm, slots, STORE)
    assert "for 4" in out


def test_guard_does_not_flag_offers_or_questions(tmp_path):
    db = tmp_path / "s.db"
    _, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    for reply in ("I can offer sat at 12:00 — do any of those work?",
                  "Just to confirm: a table for 2 on fri at 19:00 — "
                  "shall I book it?",
                  "Which works for you — sat at 12:00 or sat at 19:00?"):
        assert check_reply(reply, wm, slots, STORE)["ok"], reply


# ------------------------------------------------------------ rules -------

def test_under_n_means_n_minus_one():
    assert auto_book_max_party(
        "auto-book parties under 5") == 4
    assert auto_book_max_party(
        "auto-book fewer than 5 people") == 4
    assert auto_book_max_party(
        "auto-book less than 3") == 2
    assert auto_book_max_party(
        "auto-book < 5") == 4


def test_inclusive_bounds_keep_n():
    assert auto_book_max_party(
        "auto-book parties of 4 or fewer") == 4
    assert auto_book_max_party(
        "auto-book up to 4 without confirming") == 4
    assert auto_book_max_party(
        "auto-book at most 4") == 4
    assert auto_book_max_party(
        "auto-book <= 4") == 4
    assert auto_book_max_party(
        "auto-book no more than 6") == 6


def test_under_n_rule_blocks_party_of_n(tmp_path):
    """Owner said 'under 5' — a party of 5 must still confirm."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    store.owner_correct(STORE, "auto-book parties under 5",
                        source_text="auto-book parties under 5")
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "Table for five on Friday at 7 PM.")
    assert "shall i book" in reply.lower()       # still gated
    wm2 = pipe.start_call("+2")
    reply = pipe.turn(wm2, "Table for four on Friday at 6 PM.")
    assert "all set" in reply.lower()            # 4 auto-books


# ------------------------------------------------- memory hygiene ---------

def test_discard_purges_requests_and_turns(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM.")
    pipe.turn(wm, "Yes.")
    pipe.end_call(wm)
    assert wm.turns == [] and wm.requests == [] and wm.tool_results == []


def test_memory_load_returns_open_bookings(tmp_path):
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    slots.reserve(STORE, "fri-1900", CALLER, 2)
    slots.reserve(STORE, "sat-1200", CALLER, 4)
    res = dispatch(ctx, "memory_load", {"caller_id": CALLER})
    held = {b["slot_id"] for b in res["open_bookings"]}
    assert held == {"fri-1900", "sat-1200"}


def test_concurrent_memory_load_one_wm(tmp_path):
    """Two simultaneous memory_load calls for one caller must not create
    two working memories."""
    db = tmp_path / "s.db"
    store, slots, _ = _pipe(db)
    ctx = ToolContext(store=store, slots=slots, store_id=STORE)
    results = []

    def load():
        results.append(dispatch(ctx, "memory_load",
                                {"caller_id": CALLER})["call_id"])

    threads = [threading.Thread(target=load) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert len(ctx.active_calls) == 1


# ------------------------------------------------------ server races ------

def test_turn_after_end_returns_410(tmp_path):
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", seed_slots=["fri-1900"])
    c = TestClient(app)
    sid = c.post("/call/start").json()["session_id"]
    c.post(f"/call/{sid}/end")
    r = c.post(f"/call/{sid}/turn", json={"text": "hello"})
    assert r.status_code in (404, 410)


def test_spend_cap_atomic_under_concurrency(tmp_path):
    """A burst of concurrent /call/start requests can't jointly exceed the
    cap — try_reserve serializes the check+book."""
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", spend_cap_usd=0.30,
                     seed_slots=["fri-1900"])
    c = TestClient(app)
    codes = []

    def start():
        codes.append(c.post("/call/start").status_code)

    threads = [threading.Thread(target=start) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 300s reservation at $4.50/hr = $0.375 > $0.30 cap -> exactly one
    # session may start; the other nine get 429.
    assert codes.count(200) == 1
    assert codes.count(429) == 9


def test_availability_question_free_slot_books(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "Is Saturday at 7 PM open?")
    assert "is open" in reply
    reply = pipe.turn(wm, "For two, name is Dana.")
    pipe.turn(wm, "Yes.")
    assert slots.get(STORE, "sat-1900")["held_by"] == CALLER


def test_availability_question_taken_slot_offers(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    slots.reserve(STORE, "sat-1900", "+other", 2)
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "Is Saturday at 7 PM open?")
    assert "taken" in reply
    assert "sat at 12:00" in reply


def test_general_question_with_weekday_is_inquiry(tmp_path):
    """'Do you open on Sundays?' parses a weekday but must not be steered
    into the booking flow."""
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    reply = pipe.turn(wm, "Do you open on Sundays?")
    assert "noted it for the owner" in reply
    rep = pipe.end_call(wm)
    assert rep["category"] == "callback_needed"
    assert any("inquiry" in u for u in wm.unresolved)


def test_call_page_serves_browser_mic_client(tmp_path):
    """P0-8 surface: /call serves the browser-mic page without auth and it
    wires the real session endpoints."""
    app = create_app(store_path=str(tmp_path / "s.db"), store_id=STORE,
                     api_key="sekret", seed_slots=["fri-1900"])
    c = TestClient(app)
    r = c.get("/call")
    assert r.status_code == 200
    html = r.text
    assert "/call/start" in html and "/turn" in html and "/end" in html
    assert "SpeechRecognition" in html


def test_multi_booking_report_summary_covers_all(tmp_path):
    db = tmp_path / "s.db"
    store, slots, pipe = _pipe(db)
    wm = pipe.start_call(CALLER)
    pipe.turn(wm, "Table for two on Friday at 7 PM, name is Dana.")
    pipe.turn(wm, "Yes.")
    pipe.turn(wm, "And a table for four on Saturday at noon.")
    pipe.turn(wm, "Yes.")
    rep = pipe.end_call(wm)
    assert "fri-1900" in rep["summary"] and "sat-1200" in rep["summary"]
    assert "(None)" not in rep["summary"]
