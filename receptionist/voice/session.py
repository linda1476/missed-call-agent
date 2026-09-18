"""Voice Agent API session client (production path).

Single WebSocket: audio in, audio out. Protocol per the events reference
(docs/voice-agents/voice-agent-api/events-reference):

  connect -> session.update -> session.ready -> stream input.audio (PCM16)
  -> tool.call arrives (arguments is a dict) -> queue tool.result and send
  it once reply.done is the latest event -> session.end -> session.ended.

`tool.result` fields per docs: `call_id` (echoed from tool.call), `result`
as a JSON STRING, optional `is_error`. Audio must not stream before
`session.ready`. A bare disconnect leaves a 30-second billable grace
window — always send `session.end` when we mean to hang up.

The same tools can instead be registered as server-side HTTP tools pointing
at tools/server.py (see DECISIONS/D1: needs an API key to run live).
"""

import asyncio
import base64
import json
import os
from pathlib import Path

_WS_URL = "wss://agents.assemblyai.com/v1/ws"
_SCHEMAS = Path(__file__).resolve().parent.parent.parent / "schemas"


def load_tool_schemas() -> list[dict]:
    """Tool definitions come from exactly one place: schemas/."""
    tools = []
    for name in ("memory_load", "memory_search", "booking_reserve",
                 "booking_release", "report_emit"):
        tools.append(json.loads((_SCHEMAS / f"{name}.json").read_text(encoding="utf-8")))
    return tools


def build_session_update(system_prompt: str = "", greeting: str = "",
                         keyterms: list[str] | None = None,
                         agent_id: str | None = None) -> dict:
    """Inline session config. `agent_id` binds a stored agent instead —
    per docs it is mutually exclusive with the inline fields."""
    if agent_id:
        return {"type": "session.update", "session": {"agent_id": agent_id}}
    session: dict = {
        "system_prompt": system_prompt,
        "greeting": greeting,
        "tools": load_tool_schemas(),
        "input": {"format": {"encoding": "audio/pcm"}},
    }
    if keyterms:
        session["input"]["keyterms"] = keyterms
    return {"type": "session.update", "session": session}


async def run_session(pcm_source, dispatch, api_key: str | None = None,
                      system_prompt: str = "You are a concise receptionist.",
                      greeting: str = "Thanks for calling!",
                      agent_id: str | None = None,
                      keyterms: list[str] | None = None,
                      reply_guard=None,
                      on_end=None):
    """Drive one Voice Agent session. `pcm_source` yields PCM16 bytes;
    `dispatch(name, args) -> dict` runs tool calls (tools/handlers.py).
    `reply_guard` (optional) is called with each final agent transcript;
    when it returns correction instructions they are sent via reply.create —
    the deterministic contradiction check from the memory spec.
    `on_end` (optional, sync or async) runs after session.ended/disconnect —
    the production harness uses it to run post-call handoff for any call the
    agent did not close via report_emit.
    Raises RuntimeError without a key — see DECISIONS.md D1."""
    key = api_key or os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set — see DECISIONS.md D1")
    import websockets

    async with websockets.connect(
        _WS_URL, additional_headers={"Authorization": f"Bearer {key}"}
    ) as ws:
        await ws.send(json.dumps(
            build_session_update(system_prompt, greeting,
                                 keyterms=keyterms, agent_id=agent_id)))

        ready = asyncio.Event()
        ended = False
        pending_results: list[dict] = []

        async def send_audio():
            # Docs: stream input.audio only after session.ready.
            await ready.wait()
            async for chunk in pcm_source:
                await ws.send(json.dumps({
                    "type": "input.audio",
                    "audio": base64.b64encode(chunk).decode(),
                }))

        async def drain_results():
            for r in pending_results:
                await ws.send(json.dumps(r))
            pending_results.clear()

        sender = asyncio.create_task(send_audio())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "session.ready":
                    ready.set()
                elif t == "tool.call":
                    # Docs: accumulate on tool.call, drain inside reply.done.
                    result = dispatch(msg.get("name") or "",
                                      msg.get("arguments") or {})
                    pending_results.append({
                        "type": "tool.result",
                        "call_id": msg.get("call_id"),
                        "result": json.dumps(result),
                        "is_error": isinstance(result, dict)
                                    and result.get("ok") is False,
                    })
                elif t == "reply.done":
                    if msg.get("status") == "interrupted":
                        # Docs: a barged-in reply discards the tool.result
                        # accumulators from the just-ended reply.
                        pending_results.clear()
                    else:
                        await drain_results()
                elif t == "transcript.agent":
                    # interrupted=True carries only the spoken fragment —
                    # not a completed claim, so the guard doesn't judge it.
                    if reply_guard is not None and \
                            not msg.get("interrupted"):
                        note = reply_guard(msg.get("text") or "")
                        if note:
                            await ws.send(json.dumps({
                                "type": "reply.create",
                                "instructions": note,
                            }))
                elif t == "session.error":
                    raise RuntimeError(
                        f"session.error {msg.get('code')}: {msg.get('message')}")
                elif t == "session.ended":
                    ended = True
                    break
        finally:
            sender.cancel()
            # Clean teardown stops billing immediately; a bare disconnect
            # leaves a 30s grace window that still costs money.
            if not ended:
                try:
                    await ws.send(json.dumps({"type": "session.end"}))
                except Exception:
                    pass
            if on_end is not None:
                maybe = on_end()
                if asyncio.iscoroutine(maybe):
                    await maybe
