"""HTTP tool server — the server-side HTTP-tools path for the Voice Agent
API. AssemblyAI calls these endpoints directly (T0-1).

    POST /tools/{name}        {"arguments": {...}} -> tool result JSON
    GET  /health              liveness (always open)
    GET  /call/start          open a tracked call session (P0-8 liveness)
    GET  /call/{sid}/status   {"alive": bool}
    POST /call/{sid}/end      close the session
    GET  /dashboard           owner-facing action feed
    POST /dashboard/correct   owner correction -> procedural rule

Auth: when TOOLS_API_KEY is set, every endpoint except /health and the
anonymous /call/* session endpoints requires `Authorization: Bearer <key>`
(or `?key=<key>` for browser access to the dashboard). Configure the same
header on the HTTP tools in the AssemblyAI session. When unset the server
runs in dev mode (all open) and logs a warning — set it for any deployment.

Spend: when DAILY_SPEND_CAP_USD > 0, /call/start returns 429 once the day's
accumulated session seconds exceed the cap at $4.50/hr.
"""

import logging
import os
import time
import uuid
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..booking.slots import SlotTable
from ..dashboard.app import render_dashboard
from ..memory.store import MemoryStore
from .handlers import ToolContext, dispatch

log = logging.getLogger("missed-call-agent")

_SESSION_TTL_S = 300      # a tracked session stays alive up to 5 minutes
_RATE_PER_HOUR = 4.50     # Voice Agent API all-in hourly price


class _SpendTracker:
    """Accumulates session seconds per UTC day against a USD cap."""

    def __init__(self, cap_usd: float):
        self.cap = cap_usd
        self.day = date.today()
        self.seconds = 0.0

    def _roll(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.seconds = 0.0

    def ok(self) -> bool:
        self._roll()
        return self.seconds * _RATE_PER_HOUR / 3600 < self.cap

    def charge(self, seconds: float) -> None:
        self._roll()
        self.seconds += max(0.0, seconds)


def create_app(store_path: str = "./data/store.db",
               store_id: str = "default",
               api_key: str | None = None,
               spend_cap_usd: float | None = None) -> FastAPI:
    ctx = ToolContext(store=MemoryStore(store_path),
                      slots=SlotTable(store_path), store_id=store_id)
    if api_key is None:
        api_key = os.environ.get("TOOLS_API_KEY", "")
    if spend_cap_usd is None:
        spend_cap_usd = float(os.environ.get("DAILY_SPEND_CAP_USD", "0") or 0)
    spend = _SpendTracker(spend_cap_usd)
    sessions: dict[str, dict] = {}
    if not api_key:
        log.warning("TOOLS_API_KEY unset — dev mode: tools/dashboard open")

    app = FastAPI(title="missed-call-agent tools")
    app.state.spend = spend
    app.state.sessions = sessions

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        path = request.url.path
        if not api_key or path == "/health" or path.startswith("/call"):
            return await call_next(request)
        if request.headers.get("authorization") == f"Bearer {api_key}" or \
                request.query_params.get("key") == api_key:
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

    # ---- call-session liveness (P0-8; anonymous, exposes no caller data) ----

    def _sweep_sessions() -> None:
        now = time.time()
        for sid, s in list(sessions.items()):
            if now >= s["ends_at"]:
                if not s["charged"]:
                    spend.charge(s["ends_at"] - s["created"])
                del sessions[sid]

    @app.api_route("/call/start", methods=["GET", "POST"])
    def call_start():
        _sweep_sessions()
        if spend.cap and not spend.ok():
            return JSONResponse(
                {"ok": False, "error": "daily spend cap reached"},
                status_code=429)
        sid = f"sess_{uuid.uuid4().hex[:12]}"
        now = time.time()
        sessions[sid] = {"created": now, "ends_at": now + _SESSION_TTL_S,
                         "charged": False}
        return {"session_id": sid, "ttl": _SESSION_TTL_S}

    @app.get("/call/{sid}/status")
    def call_status(sid: str):
        s = sessions.get(sid)
        if s is None:
            return JSONResponse({"alive": False, "session_id": sid},
                                status_code=404)
        alive = time.time() < s["ends_at"]
        if not alive and not s["charged"]:
            spend.charge(s["ends_at"] - s["created"])
            s["charged"] = True
        return {"alive": alive, "session_id": sid}

    @app.post("/call/{sid}/end")
    def call_end(sid: str):
        s = sessions.pop(sid, None)
        if s is not None and not s["charged"]:
            spend.charge(min(time.time(), s["ends_at"]) - s["created"])
        return {"ok": True}

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


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    app = create_app(store_path=os.environ.get("STORE_PATH", "./data/store.db"),
                     store_id=os.environ.get("STORE_ID", "default"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
