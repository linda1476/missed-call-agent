# Video storyboard (≤5 min MP4) — draft

1. **Problem** (0:00–0:20) — one line + number: "Small businesses miss ~28–62%
   of calls; 85% of those callers never call back." (industry-cited figure on
   slide; CallRail-measured 28% as conservative anchor)
2. **First call** (0:20–1:10) — new caller books a table; agent greets, takes
   details, confirms; booking lands on the owner dashboard.
3. **Repeat call** (1:10–2:00) — same number calls back; agent greets with the
   prior booking already loaded ("party of 2, Friday 7 PM") and moves it to
   Saturday — cross-call memory on screen.
4. **Concurrent calls** (2:00–2:50) — split screen: two callers ask for the
   last Saturday 7 PM slot at once; one confirms, the other gets alternatives
   (CAS lock, 0 duplicates).
5. **Owner correction** (2:50–3:40) — owner types "auto-book parties of 4 or
   fewer without confirming"; next identical call books with no confirmation
   step. Procedural memory, source shown.
6. **Architecture** (3:40–4:10) — 30s diagram: Voice Agent API tool.call →
   memory server (3 memory kinds) → CAS slot lock. Call out where each
   AssemblyAI feature is used (tool calls, keyterms, turn-taking/VAD).
7. **Close** (4:10–4:30) — differentiators: cross-call memory · concurrent
   slot lock · owner-correction procedural rules. MIT repo URL.

Note: any recorded calls are labeled "recorded".
