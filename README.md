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
lock. Losers of a race get alternatives offered, and an accepted offer
books without a redundant second confirmation. A caller may hold several
slots; the slot table (not memory) is authoritative for what they hold.
Moves are atomic: `booking_reserve`'s `swap_from` releases the old slot in
the same transaction, after the new CAS succeeds — a failed move keeps the
original. `SLOT_PER_CALLER_CAP` (default 4) bounds how many slots one
caller can hold, and a swap doesn't count as an extra booking against it.

The memory spec's contradiction check is real code, not a prompt: after
every reply, `voice/guard.py` deterministically compares claimed bookings/
cancellations against the slot table and regenerates the reply when they
diverge — wired in `pipeline.turn`, and available to the production WS
path via `run_session(reply_guard=make_reply_guard(wm, slots, store_id))`
(it fires `reply.create` with correction instructions on `transcript.agent`).

## AssemblyAI features used

- Voice Agent API WebSocket session at `wss://agents.assemblyai.com/v1/ws`:
  `session.update` w/ tools + keyterms → `session.ready` gate →
  `input.audio` → `tool.call` (`call_id`) → `tool.result` (JSON-string
  result drained on `reply.done`, discarded on `status:"interrupted"`) →
  `session.end` (stops billing immediately — a bare disconnect costs a
  30-second grace window)
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

Endpoints: `POST /tools/{name}` (HTTP tools for AssemblyAI: `memory_load`,
`memory_search`, `booking_reserve`, `booking_release`, `report_emit`),
`POST /call/start` + `POST /call/{sid}/turn` + `POST /call/{sid}/end` +
`GET /call/{sid}/status` (real call sessions through the dialogue engine —
a WorkingMemory opens at start, turns drive the responder, end/expiry runs
the handoff), `GET /call` (browser-mic demo page: SpeechRecognition → the
session endpoints → SpeechSynthesis, no extra infra), `GET /dashboard`
(owner feed) + `POST /dashboard/correct` (owner rule).

**Deploying:** set `TOOLS_API_KEY` — every endpoint except `/health` and the
anonymous `/call/*` session endpoints then requires `Authorization: Bearer
<key>` (or `?key=` for the browser dashboard). `?caller_id=` on
`/call/start` is honored only when the request carries the key — anonymous
sessions get anonymous caller ids, so a stranger can't read or cancel
someone else's bookings. Configure the same header on the HTTP tools in
the AssemblyAI session. Unset = dev mode, all open.
`SEED_SLOTS` seeds the shop's bookable slots (required — an empty slot
table can't take bookings). `DAILY_SPEND_CAP_USD` stops new sessions once
the day's usage estimate (session seconds at $4.50/hr) passes the cap —
the check+reservation is atomic so bursts can't overshoot. `SHOP_TZ`
(IANA name, e.g. `America/New_York`) makes "tomorrow"/"tonight" resolve
against the shop's local date rather than the host's; `SLOT_PER_CALLER_CAP`
(default 4, 0 = unlimited) bounds simultaneous holds per caller.

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
