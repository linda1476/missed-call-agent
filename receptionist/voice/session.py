"""Voice Agent API session client (production path).

Single WebSocket: audio in, audio out. Tool calls arrive as `tool.call`
events and are dispatched to our handlers (memory_load, memory_search,
booking_reserve, report_emit) — the client-side function-tool path. The
same tools can instead be registered as server-side HTTP tools pointing at
tools/server.py (see DECISIONS/D1: needs an API key to run live).

Protocol per docs: connect -> session.update -> session.ready -> stream
input.audio (~50ms PCM16 chunks) -> handle tool.call -> tool.result ->
session.end -> session.ended.
"""

import asyncio
import base64
import json
import os
from pathlib import Path

_WS_URL = "wss://api.assemblyai.com/voice-agent/v1/ws"
_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"


def load_tool_schemas() -> list[dict]:
    """Tool definitions come from exactly one place: schemas/."""
    tools = []
    for name in ("memory_load", "memory_search", "booking_reserve", "report_emit"):
        tools.append(json.loads((_SCHEMAS / f"{name}.json").read_text(encoding="utf-8")))
    return tools


def build_session_update(system_prompt: str, greeting: str) -> dict:
    return {
        "type": "session.update",
        "session": {
            "system_prompt": system_prompt,
            "greeting": greeting,
            "tools": load_tool_schemas(),
            "input": {"format": {"encoding": "audio/pcm"}},
        },
    }


async def run_session(pcm_source, dispatch, api_key: str | None = None,
                      system_prompt: str = "You are a concise receptionist.",
                      greeting: str = "Thanks for calling!",
                      on_end=None):
    """Drive one Voice Agent session. `pcm_source` yields PCM16 bytes;
    `dispatch(name, args) -> dict` runs tool calls (tools/handlers.py).
    `on_end` (optional, sync or async) runs after session.ended/disconnect —
    the production harness uses it to run post-call handoff for any call the
    agent did not close via report_emit.
    Raises RuntimeError without a key — see DECISIONS.md D1."""
    import websockets

    key = api_key or os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set — see DECISIONS.md D1")

    async with websockets.connect(
        _WS_URL, additional_headers={"Authorization": key}
    ) as ws:
        await ws.send(json.dumps(build_session_update(system_prompt, greeting)))

        async def send_audio():
            async for chunk in pcm_source:
                await ws.send(json.dumps({
                    "type": "input.audio",
                    "audio": base64.b64encode(chunk).decode(),
                }))

        sender = asyncio.create_task(send_audio())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "tool.call":
                    result = dispatch(msg["name"], msg.get("arguments", {}))
                    await ws.send(json.dumps({
                        "type": "tool.result",
                        "tool_call_id": msg.get("tool_call_id"),
                        "result": result,
                    }))
                elif t == "session.ended":
                    break
        finally:
            sender.cancel()
            if on_end is not None:
                maybe = on_end()
                if asyncio.iscoroutine(maybe):
                    await maybe
