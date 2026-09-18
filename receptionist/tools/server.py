"""HTTP tool server — the server-side HTTP-tools path for the Voice Agent
API. AssemblyAI calls these endpoints directly (T0-1).

    POST /tools/{name}        {"arguments": {...}} -> tool result JSON
    GET  /health              liveness (always open)
    POST /call/start          open a real call session (greeting + WorkingMemory);
                              ?caller_id= is honored only with API-key auth —
                              anonymous sessions get anonymous caller ids
    GET  /call/{sid}/status   {"alive": bool}
    POST /call/{sid}/turn     {"text": ...} -> agent reply (the call itself)
    POST /call/{sid}/end      run post-call handoff, return the owner report
    GET  /dashboard           owner-facing action feed
    POST /dashboard/correct   owner correction -> procedural rule

/call sessions are real calls through the dialogue engine — a WorkingMemory
is opened at start, each /turn drives the responder (memory + CAS booking),
and /end runs the fixed handoff (expired sessions hand off too). Sessions
are anonymous: without the API key they cannot adopt a real caller_id, so
a public deployment can't leak or mutate another caller's memory. The
AssemblyAI audio layer connects separately via the HTTP tools above.

Auth: when TOOLS_API_KEY is set, every endpoint except /health and the
anonymous /call/* session endpoints requires `Authorization: Bearer <key>`
(or `?key=<key>` for browser access to the dashboard). Configure the same
header on the HTTP tools in the AssemblyAI session. When unset the server
runs in dev mode (all open) and logs a warning — set it for any deployment.

Spend: when DAILY_SPEND_CAP_USD > 0, /call/start returns 429 once the day's
accumulated session seconds exceed the cap at $4.50/hr. SEED_SLOTS (comma-
separated slot ids) initializes the shop's bookable inventory at startup.
"""

import hmac
import logging
import os
import re
import threading
import time
import uuid
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..booking.slots import SlotTable
from ..dashboard.app import render_dashboard
from ..memory.store import MemoryStore
from ..voice.pipeline import CallPipeline
from .handlers import ToolContext, dispatch

log = logging.getLogger("missed-call-agent")

_SESSION_TTL_S = 300      # idle timeout — each /turn slides it forward
_SESSION_MAX_S = 3600     # hard cap on total session length
_MAX_SESSIONS = 100       # bound anonymous session state
_RATE_PER_HOUR = 4.50     # Voice Agent API all-in hourly price
_CALLER_ID_RE = re.compile(r"^\+?[0-9A-Za-z_-]{4,32}$")  # same as schemas/


class _SpendTracker:
    """Accumulates session seconds per UTC day against a USD cap.

    All mutations run under `lock`; `try_reserve` checks the cap and books
    a reservation in one critical section so a burst of concurrent
    /call/start requests can't all observe "under cap" at once."""

    def __init__(self, cap_usd: float):
        self.cap = cap_usd
        self.day = date.today()
        self.seconds = 0.0
        self.lock = threading.Lock()

    def _roll(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.seconds = 0.0

    def try_reserve(self, seconds: float) -> bool:
        """Atomic cap-check + reservation. A 0/None cap disables tracking."""
        if not self.cap:
            return True
        with self.lock:
            self._roll()
            if self.seconds * _RATE_PER_HOUR / 3600 >= self.cap:
                return False
            self.seconds += max(0.0, seconds)
            return True

    def charge(self, seconds: float) -> None:
        """Signed settle: a negative value releases an earlier reservation."""
        with self.lock:
            self._roll()
            self.seconds = max(0.0, self.seconds + seconds)


def create_app(store_path: str = "./data/store.db",
               store_id: str = "default",
               api_key: str | None = None,
               spend_cap_usd: float | None = None,
               seed_slots: list[str] | None = None) -> FastAPI:
    ctx = ToolContext(store=MemoryStore(store_path),
                      slots=SlotTable(store_path), store_id=store_id)
    if seed_slots is None:
        seed_slots = [s.strip() for s in
                      os.environ.get("SEED_SLOTS", "").split(",") if s.strip()]
    if seed_slots:
        ctx.slots.seed(store_id, seed_slots)
    if api_key is None:
        api_key = os.environ.get("TOOLS_API_KEY", "")
    if spend_cap_usd is None:
        spend_cap_usd = float(os.environ.get("DAILY_SPEND_CAP_USD", "0") or 0)
    spend = _SpendTracker(spend_cap_usd)
    sessions: dict[str, dict] = {}
    session_lock = threading.Lock()
    pipe = CallPipeline(ctx.store, ctx.slots, store_id)
    if not api_key:
        log.warning("TOOLS_API_KEY unset — dev mode: tools/dashboard open")

    app = FastAPI(title="missed-call-agent tools")
    app.state.spend = spend
    app.state.sessions = sessions

    def _key_ok(request: Request) -> bool:
        """API-key check shared by the middleware and /call/start — true
        when the request carries the key (or dev mode: no key configured)."""
        if not api_key:
            return True
        auth = request.headers.get("authorization") or ""
        key_q = request.query_params.get("key") or ""
        return (hmac.compare_digest(auth, f"Bearer {api_key}") or
                hmac.compare_digest(key_q, api_key))

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        path = request.url.path
        if path in ("/health", "/call") or \
                path.startswith("/call/") or _key_ok(request):
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/tools/{name}")
    async def tool_endpoint(name: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON body"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid body"},
                                status_code=400)
        return JSONResponse(dispatch(ctx, name, body.get("arguments", body)))

    # ---- call sessions (P0-8; anonymous caller-facing surface) ----
    # Each session is a real call: a WorkingMemory opens at start, /turn
    # drives the dialogue engine, and /end (or expiry) runs the handoff.

    def _end_session(s: dict) -> None:
        """Charge + handoff, exactly once. Expiry (sweep/status/turn) and an
        explicit /end can race on the same session — the first caller claims
        it under the lock and runs the handoff; concurrent enders wait on
        the done event and share the resulting report. The session's turn
        lock is held through the handoff so an in-flight /turn finishes
        before the working memory is torn down."""
        with session_lock:
            if s["ended"]:
                claimed = False
            else:
                s["ended"] = True
                claimed = True
        if not claimed:
            s["done"].wait(10)
            return
        with s["turn_lock"]:
            if not s["charged"]:
                actual = min(time.time(), s["ends_at"]) - s["created"]
                spend.charge(actual - s["reserved"])
                s["charged"] = True
            try:
                s["report"] = pipe.end_call(s["wm"])
            except Exception:
                log.exception("handoff failed for call session")
                s["report"] = {}
            finally:
                s["done"].set()

    def _sweep_sessions() -> None:
        now = time.time()
        for sid, s in list(sessions.items()):
            if now >= s["ends_at"]:
                _end_session(s)
                sessions.pop(sid, None)

    @app.api_route("/call/start", methods=["GET", "POST"])
    def call_start(request: Request):
        _sweep_sessions()
        # Reserve the session's max idle window atomically — concurrent
        # starts can't jointly overshoot the cap; the difference between
        # the reservation and actual use is settled (never negative) at
        # session end.
        if not spend.try_reserve(_SESSION_TTL_S):
            return JSONResponse(
                {"ok": False, "error": "daily spend cap reached"},
                status_code=429)
        with session_lock:
            if len(sessions) >= _MAX_SESSIONS:
                spend.charge(-_SESSION_TTL_S)
                return JSONResponse({"ok": False, "error": "server busy"},
                                    status_code=429)
        # ?caller_id= is honored only with the API key — an anonymous
        # session that claims someone else's number could otherwise see
        # and cancel/change that caller's bookings.
        caller_id = request.query_params.get("caller_id") or ""
        if caller_id and not (_CALLER_ID_RE.match(caller_id)
                              and _key_ok(request)):
            log.info("/call/start: ignoring caller_id (bad key or format)")
            caller_id = ""
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        wm = pipe.start_call(caller_id or f"anon-{uuid.uuid4().hex[:8]}")
        greet = pipe.greeting(wm)
        wm.add_turn("agent", greet)
        with session_lock:
            sessions[sid] = {
                "created": now, "ends_at": now + _SESSION_TTL_S,
                "charged": False, "ended": False,
                "reserved": _SESSION_TTL_S,
                "done": threading.Event(),
                "turn_lock": threading.Lock(),
                "wm": wm, "report": None}
        return {"session_id": sid, "ttl": _SESSION_TTL_S, "greeting": greet}

    @app.get("/call/{sid}/status")
    def call_status(sid: str):
        s = sessions.get(sid)
        if s is None:
            return JSONResponse({"alive": False, "session_id": sid},
                                status_code=404)
        if time.time() >= s["ends_at"]:
            _end_session(s)
            sessions.pop(sid, None)
            return {"alive": False, "session_id": sid}
        return {"alive": True, "session_id": sid,
                "call_id": s["wm"].call_id}

    @app.post("/call/{sid}/turn")
    async def call_turn(sid: str, request: Request):
        s = sessions.get(sid)
        if s is None:
            return JSONResponse({"ok": False, "error": "unknown session"},
                                status_code=404)
        if time.time() >= s["ends_at"]:
            _end_session(s)
            sessions.pop(sid, None)
            return JSONResponse({"ok": False, "error": "session expired"},
                                status_code=410)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON body"},
                                status_code=400)
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "text required"},
                                status_code=400)
        # Serialize turns on one session and exclude a concurrent /end:
        # the handoff takes this same lock, so a late /turn can never land
        # on a working memory whose handoff already ran (orphan booking).
        with s["turn_lock"]:
            if s["ended"]:
                return JSONResponse(
                    {"ok": False, "error": "session ended"}, status_code=410)
            # Activity slides the idle deadline forward (hard cap
            # unchanged) — a call longer than the idle TTL isn't handed
            # off mid-conversation.
            s["ends_at"] = min(time.time() + _SESSION_TTL_S,
                               s["created"] + _SESSION_MAX_S)
            reply = pipe.turn(s["wm"], text)
        return {"ok": True, "reply": reply}

    @app.post("/call/{sid}/end")
    def call_end(sid: str):
        s = sessions.pop(sid, None)
        if s is None:
            return JSONResponse({"ok": False, "error": "unknown session"},
                                status_code=404)
        _end_session(s)
        return {"ok": True, "report": s["report"]}

    # ---- browser-mic demo page (P0-8 surface) ----
    # Mic → browser SpeechRecognition → /call/* HTTP endpoints → reply
    # spoken via SpeechSynthesis. Zero extra infra, no key exposure: the
    # page only ever talks to our own session endpoints. The production
    # audio path stays the AssemblyAI Voice Agent API.

    @app.get("/call", response_class=HTMLResponse)
    def call_page():
        return _CALL_PAGE_HTML

    # ---- owner dashboard ----

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return render_dashboard(ctx.store, ctx.store_id)

    @app.post("/dashboard/correct")
    async def correct(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON body"},
                                status_code=400)
        rule_text = str(body.get("rule_text") or "").strip()
        if not rule_text:
            return JSONResponse({"ok": False, "error": "rule_text required"},
                                status_code=400)
        rule_id = ctx.store.owner_correct(
            ctx.store_id, rule_text,
            source_text=body.get("source_text") or rule_text,
        )
        return {"ok": True, "rule_id": rule_id}

    return app


_CALL_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Missed-call agent — live demo</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:38rem;
 background:#0f1115;color:#e8eaed}
h1{font-size:1.25rem}
#log{border:1px solid #2a2f3a;border-radius:10px;padding:1rem;min-height:
 14rem;max-height:22rem;overflow-y:auto;background:#1b1f27;margin:1rem 0}
.u{color:#9ecbff}.a{color:#b8e6c3}.sys{color:#7d8794;font-size:.85rem}
.row{margin:.35rem 0}
button{padding:.6rem 1.1rem;border:0;border-radius:8px;font-size:1rem;
 cursor:pointer;margin-right:.5rem}
#mic{background:#2fbf71;color:#fff}#mic.on{background:#e05d5d}
#end{background:#4c8dff;color:#fff}#start{background:#f2a93b;color:#111}
button:disabled{opacity:.4;cursor:default}
#report{white-space:pre-wrap;font-size:.8rem;color:#9aa4b2}
</style></head><body>
<h1>Missed-call agent — live demo</h1>
<p class="sys">You are the caller; the agent answers for the shop.
Uses your browser's speech recognition + synthesis — Chrome or Edge.</p>
<button id="start">Start call</button>
<button id="mic" disabled>Talk</button>
<button id="end" disabled>End call</button>
<div id="log"></div>
<pre id="report"></pre>
<script>
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const log = (cls, txt) => {
  const d = document.createElement("div");
  d.className = "row " + cls; d.textContent = txt;
  document.getElementById("log").appendChild(d);
  d.scrollIntoView();
};
const speak = txt => {
  try { speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(txt); u.lang = "en-US";
    speechSynthesis.speak(u); } catch (e) {}
};
let sid = null, rec = null, busy = false;
const micBtn = document.getElementById("mic");
const setBusy = b => { busy = b; micBtn.disabled = b || !sid; };

document.getElementById("start").onclick = async () => {
  const r = await fetch("/call/start", {method: "POST"});
  const j = await r.json();
  if (!j.session_id) { log("sys", "Could not start: " + (j.error || "?")); return; }
  sid = j.session_id;
  document.getElementById("end").disabled = false;
  micBtn.disabled = false;
  log("a", "Agent: " + j.greeting); speak(j.greeting);
};

micBtn.onclick = () => {
  if (!SR) { log("sys", "SpeechRecognition not supported in this browser."); return; }
  if (rec) { rec.stop(); return; }
  rec = new SR(); rec.lang = "en-US"; rec.interimResults = false;
  rec.maxAlternatives = 1;
  micBtn.classList.add("on"); micBtn.textContent = "Listening…";
  rec.onresult = async ev => {
    const text = ev.results[0][0].transcript;
    log("u", "You: " + text); setBusy(true);
    try {
      const r = await fetch(`/call/${sid}/turn`, {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text})});
      const j = await r.json();
      if (j.reply) { log("a", "Agent: " + j.reply); speak(j.reply); }
      else log("sys", "Turn failed: " + (j.error || r.status));
    } finally { setBusy(false); }
  };
  rec.onend = () => { micBtn.classList.remove("on");
    micBtn.textContent = "Talk"; rec = null; };
  rec.onerror = e => { log("sys", "Mic error: " + e.error); };
  rec.start();
};

document.getElementById("end").onclick = async () => {
  if (!sid) return;
  const r = await fetch(`/call/${sid}/end`, {method: "POST"});
  const j = await r.json();
  document.getElementById("report").textContent =
    "Owner report:\\n" + JSON.stringify(j.report, null, 2);
  log("sys", "Call ended — owner report below.");
  sid = null; micBtn.disabled = true;
  document.getElementById("end").disabled = true;
};
</script></body></html>"""


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    app = create_app(store_path=os.environ.get("STORE_PATH", "./data/store.db"),
                     store_id=os.environ.get("STORE_ID", "default"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
