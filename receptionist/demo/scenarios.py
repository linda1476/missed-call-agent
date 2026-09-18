"""Scripted demo-scenario replay (P0-9). A scenario JSON drives the pipeline
with text utterances and declares expectations; the runner returns which
expectations held. Used by the 3 demo cuts:

  1. first call — new caller books a table
  2. repeat call — same number, agent recalls and changes the booking
  3. concurrent — two callers race the last slot; one wins, one gets an
     alternative

Scenario format (tests/fixtures/scenarios/*.json):
  {
    "name": "...", "seed_slots": ["fri-1900", ...],
    "calls": [
      {"caller_id": "+1...", "utterances": ["..."],
       "expect": {"replies_contain": ["..."], "booking_slot": "fri-1900",
                  "no_confirm_prompt": true}}
    ]
  }
"""

import json
import threading
from pathlib import Path

from ..booking.slots import SlotTable
from ..memory.extract import DeterministicExtractor
from ..memory.store import MemoryStore
from ..memory.handoff import run_handoff
from ..report.emit import build_report
from ..voice.pipeline import CallPipeline


def load_scenario(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _check(expect: dict, replies: list[str], booking: dict | None,
           report: dict | None) -> list[str]:
    failures = []
    blob = "\n".join(replies).lower()
    for needle in expect.get("replies_contain", []):
        if needle.lower() not in blob:
            failures.append(f"reply missing {needle!r}")
    for needle in expect.get("replies_not_contain", []):
        if needle.lower() in blob:
            failures.append(f"reply unexpectedly contains {needle!r}")
    if "booking_slot" in expect:
        got = (booking or {}).get("slot_id")
        if got != expect["booking_slot"]:
            failures.append(f"booking slot {got!r} != {expect['booking_slot']!r}")
    if expect.get("no_booking") and booking and booking.get("status") == "confirmed":
        failures.append("expected no confirmed booking")
    if expect.get("no_confirm_prompt") and "shall i book" in blob:
        failures.append("confirmation prompt appeared despite rule")
    if "report_category" in expect:
        if (report or {}).get("category") != expect["report_category"]:
            failures.append(f"report category {(report or {}).get('category')!r}"
                            f" != {expect['report_category']!r}")
    return failures


def run_scenario(scenario: dict, store_path: str, store_id: str = "demo") -> dict:
    """Execute a scenario end-to-end; returns {name, ok, failures, calls}."""
    store = MemoryStore(store_path)
    slots = SlotTable(store_path)
    slots.seed(store_id, scenario.get("seed_slots", []))
    pipe = CallPipeline(store, slots, store_id,
                        extractor=DeterministicExtractor())

    calls_out, failures = [], []

    def one_call(call: dict) -> dict:
        wm = pipe.start_call(call["caller_id"])
        res = pipe.run_utterances(wm, call["utterances"])
        res.report = pipe.end_call(wm)
        return res

    if scenario.get("concurrent"):
        results: dict[int, object] = {}

        def worker(i):
            results[i] = one_call(scenario["calls"][i])

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(len(scenario["calls"]))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ordered = [results[i] for i in range(len(scenario["calls"]))]
    else:
        ordered = [one_call(c) for c in scenario["calls"]]

    for call, res in zip(scenario["calls"], ordered):
        fails = _check(call.get("expect", {}), res.replies, res.booking,
                       res.report)
        failures.extend(fails)
        calls_out.append({
            "caller_id": call["caller_id"],
            "replies": res.replies,
            "booking": res.booking,
            "report": res.report,
            "ok": not fails,
        })

    # Scenario-level expectations — used for races where which caller wins
    # is nondeterministic; assert on the combined outcome instead.
    sexp = scenario.get("expect", {})
    if sexp:
        winners = [c for c in calls_out
                   if (c["booking"] or {}).get("status") == "confirmed"]
        if "exactly_one_confirmed" in sexp:
            if len(winners) != 1:
                failures.append(f"{len(winners)} confirmed bookings, expected 1")
        if "winning_slot" in sexp and winners:
            if winners[0]["booking"]["slot_id"] != sexp["winning_slot"]:
                failures.append("winning slot mismatch")
        if sexp.get("loser_offered_alternative"):
            losers = [c for c in calls_out if c not in winners]
            for loser in losers:
                blob = "\n".join(loser["replies"]).lower()
                if "just taken" not in blob and "offer" not in blob:
                    failures.append("losing caller got no alternative offer")

    return {"name": scenario["name"], "ok": not failures,
            "failures": failures, "calls": calls_out}
