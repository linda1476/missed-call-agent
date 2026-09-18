"""Post-call handoff. Fixed order, exactly:

    call ends -> summary (history append)
              -> facts (current-slot overwrite)
              -> state update (booking confirm/cancel reflected)
              -> owner report

Procedural rules are NEVER created here — that is exclusively the
owner-correction path. The raw transcript is discarded, not persisted
(privacy minimization, documented in README).
"""

from .extract import Extractor
from .store import MemoryStore
from .working import WorkingMemory


def run_handoff(store: MemoryStore, wm: WorkingMemory, extractor: Extractor,
                report_builder=None) -> dict:
    """Execute the 4-stage handoff and return the owner report dict."""
    summary = extractor.summarize(wm)
    store.append_history(wm.store_id, wm.caller_id, "call_summary", summary,
                         meta={"call_id": wm.call_id})

    for key, value in extractor.extract_facts(wm).items():
        store.set_current(wm.store_id, wm.caller_id, key, value)

    # state update: the booking table is already authoritative via CAS during
    # the call; here we record the outcome as an immutable history record.
    b = wm.confirmed.get("booking")
    if b:
        store.append_history(
            wm.store_id, wm.caller_id, "booking_record",
            f"booking {b['status']} {b['slot_id']} party={b['party_size']}",
            meta={"call_id": wm.call_id, "slot_id": b["slot_id"],
                  "status": b["status"]},
        )

    report = report_builder(wm, extractor) if report_builder else None
    if report:
        store.append_history(wm.store_id, wm.caller_id, "owner_report",
                             report["summary"], meta=report)

    wm.discard()
    return report or {}
