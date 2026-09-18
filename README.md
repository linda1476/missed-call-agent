# missed-call-agent

A voice agent that answers missed calls for small shops (English demo: US
restaurant/salon scenarios). It takes bookings and inquiries, **remembers
callers across calls**, prevents double-booking when calls arrive
simultaneously, and sends the owner action-unit reports. MIT licensed.

Built on the **AssemblyAI Voice Agent API** (single WebSocket: STT + LLM +
TTS + turn-taking + JSON-Schema tool calls).

## Architecture

```
 caller ──audio──► AssemblyAI Voice Agent API ──tool.call──► this repo
                    (STT·LLM·TTS·VAD)            (HTTP tools or WS function tools)
                                                        │
              ┌─────────────────────────────────────────┤
              ▼                                         ▼
        memory.load/search                    booking.reserve (CAS)
              │                                         │
        ┌─────┴──────────────────────────┐    slot table (1 row lock)
        │  long-term memory (SQLite)     │
        │  · current-value slots         │  working memory (per call,
        │  · append-only history         │    discarded after handoff)
        │  · procedural rules (owner)    │
        └─────┬──────────────────────────┘
              ▼ post-call handoff (fixed order)
        summary→history · facts→current · state→slots · owner report
```

Memory is two layers. Long-term memory survives calls and has **three**
storage kinds — current-value slots (overwrite; always in prompt; wins
conflicts), append-only history (search-only; never preloaded; enforced by
DB triggers), and procedural rules (created ONLY via owner correction, each
with its source). Working memory lives for one call: loaded slots+rules,
requests, confirmations, tool results — destroyed after handoff. **Raw
transcripts are never persisted** (privacy minimization).

Booking uses compare-and-set on a single slot row — never a store-wide
lock. Losers of a race get alternatives offered.

## AssemblyAI features used

- Voice Agent API WebSocket session (`session.update` w/ tools, `input.audio`,
  `tool.call`/`tool.result`, `session.end`)
- Server-side **HTTP tools** pointing at `POST /tools/{name}` on this server
  (client-side function tools also supported via `voice/session.py`)
- Tool-call JSON Schemas for accuracy + turn-taking (schemas/ is the single
  source of truth for both server and prompt)
- keyterms prompting → recognition of shop/menu/staff names (P0-7; offline
  analogue uses faster-whisper `hotwords`)

## Install

```bash
pip install -r requirements.txt
cp .env.example .env   # set ASSEMBLYAI_API_KEY for live calls
```

## Run

```bash
python -m receptionist.tools.server          # tool server + owner dashboard (:8000)
pytest tests/acceptance -k "not p0_8"        # acceptance suite (offline)
python tests/fixtures/generate_fixtures.py   # regenerate audio fixtures (needs edge-tts)
```

Endpoints: `POST /tools/{name}` (HTTP tools for AssemblyAI),
`GET /call/start` + `GET /call/{sid}/status` (session liveness, P0-8),
`GET /dashboard` (owner feed) + `POST /dashboard/correct` (owner rule).

**Deploying:** set `TOOLS_API_KEY` — every endpoint except `/health` and the
anonymous `/call/*` liveness endpoints then requires `Authorization: Bearer
<key>` (or `?key=` for the browser dashboard). Configure the same header on
the HTTP tools in the AssemblyAI session. Unset = dev mode, all open.
`DAILY_SPEND_CAP_USD` stops new sessions once the day's usage estimate
(session seconds at $4.50/hr) passes the cap.

The acceptance suite is the contract: `tests/acceptance/test_p0_*` — one
test per task, unmodified once written.

## Repo layout

- `schemas/` — tool JSON-Schemas (single source of truth) + report schema
- `receptionist/memory/` — store / working memory / handoff / extractor / rules
- `receptionist/booking/` — CAS slot table
- `receptionist/voice/` — pipeline, responder, STT, Voice Agent session client
- `receptionist/tools/` — tool dispatch + FastAPI HTTP server
- `receptionist/dashboard/` — owner action feed + correction box
- `receptionist/demo/` — scripted scenario replay (3 demo cuts)
- `tests/acceptance/` — immutable acceptance tests; `tests/fixtures/` — audio + scenarios
- `submission/` — slides.pdf, cover.png, video_storyboard.md, descriptions
- `TASKS.md` `LABNOTE.md` `DECISIONS.md` `REPORT.md` — build governance

## Notes

- English-language demo only (Voice Agent API input languages: 18, no Korean).
- Deterministic extraction is the default for post-call handoff — AssemblyAI
  LLM Gateway tokens bill separately and aren't covered by free credits.
- Costs: Voice Agent API is $4.50/hr all-in, billed per WebSocket-open second.
