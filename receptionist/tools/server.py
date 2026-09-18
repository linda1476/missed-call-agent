"""HTTP tool server — the server-side HTTP-tools path for the Voice Agent
API. AssemblyAI calls these endpoints directly (T0-1).

    POST /tools/{name}   {"arguments": {...}} -> tool result JSON
    GET  /health
    GET  /dashboard      owner-facing action feed
    POST /dashboard/correct   owner correction -> procedural rule
"""

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..booking.slots import SlotTable
from ..dashboard.app import render_dashboard
from ..memory.store import MemoryStore
from .handlers import ToolContext, dispatch


def create_app(store_path: str = "./data/store.db",
               store_id: str = "default") -> FastAPI:
    ctx = ToolContext(store=MemoryStore(store_path),
                      slots=SlotTable(store_path), store_id=store_id)
    app = FastAPI(title="missed-call-agent tools")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/tools/{name}")
    async def tool_endpoint(name: str, request: Request):
        body = await request.json()
        return JSONResponse(dispatch(ctx, name, body.get("arguments", body)))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return render_dashboard(ctx.store, ctx.store_id)

    @app.post("/dashboard/correct")
    async def correct(request: Request):
        body = await request.json()
        rule_id = ctx.store.owner_correct(
            ctx.store_id, body["rule_text"],
            source_text=body.get("source_text") or body["rule_text"],
        )
        return {"ok": True, "rule_id": rule_id}

    return app


def main():
    import uvicorn
    app = create_app(store_path=os.environ.get("STORE_PATH", "./data/store.db"),
                     store_id=os.environ.get("STORE_ID", "default"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
