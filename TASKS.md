# TASKS

Statuses: `todo` → (`in-progress`) → `review` → `done`. `blocked` = needs human or external input.
Completion is judged only by `tests/acceptance/<id>_*` passing. Loop order = top-down.

| id | status | task | acceptance test |
|----|--------|------|-----------------|
| T0-1 | review | Voice Agent API tool call → external HTTP endpoint usable? | doc-verified: HTTP tools (server-side) + function tools both exist; live smoke folds into P0-1 production path when key lands |
| T0-2 | review | Concurrent sessions / rate limits for 2 simultaneous calls | doc-verified: paid=100 new sess/min + autoscale, free=5; no hard cap on open streams; live confirm w/ key |
| T0-3 | done | Is LLM billed from credits? | YES-included: $4.50/hr covers built-in STT+LLM+TTS+tools. Separate: explicit LLM Gateway calls billed per-token, NOT covered by free credits → deterministic extractor is default |
| P0-1 | review | One call e2e: greeting → booking intake → hang up | `test_p0_1_call_to_booking.py` PASS — recorded WAV → booking JSON (fri-1900, party 2, Dana) via offline Whisper path; Voice Agent API path pending key (D1) |
| P0-2 | review | Memory server + tool-call wiring | `test_p0_2_cross_call_memory.py` PASS — call 2 loads prior booking into greeting + memory_search hits |
| P0-3 | review | Post-call handoff: summary/facts/state | `test_p0_3_handoff_separation.py` PASS — 3 kinds separated, raw transcript purged |
| P0-4 | review | Concurrent call slot lock (CAS) | `test_p0_4_concurrent_booking.py` PASS — 100 concurrent, exactly 1 winner, 0 duplicates, losers get alternatives |
| P0-5 | review | Owner report → web dashboard | `test_p0_5_owner_report.py` PASS — report schema-valid, dashboard renders |
| P0-6 | review | Owner correction → procedural rule | `test_p0_6_owner_correction.py` PASS — rule w/ source, same scenario auto-books, no confirm |
| P0-7 | review | Keyterm prompting improves name/menu recognition | `test_p0_7_keyterms.py` PASS — 20 proper nouns: 45% → 75% with hotwords, recorded in keyterms_report.json |
| P0-8 | blocked | Public URL, browser mic call, unattended | `test_p0_8_hosted_url.py` FAILS (expected): needs PUBLIC_URL + deployment; blocked on D1 (API key) + D3 (host) |
| P0-9 | review | 3 demo scenarios auto-replay | `test_p0_9_demo_scenarios.py` PASS ×3 — first call / repeat w/ memory / concurrent race |
| P0-10 | review | Submission: README/slides/storyboard/cover | `test_p0_10_submission_files.py` PASS — all artifacts exist, README quickstart documented |

Suite: 13/14 pass; only P0-8 fails (deployment genuinely not configured).
Hold rules: P0-6, P0-7 only after P0-1..P0-5 reach `review`. If P0-5 not `review` by 2026-09-27, drop P0-6/P0-7 and focus on P0-8..P0-10.
