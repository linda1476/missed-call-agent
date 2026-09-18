"""P0-4: 100 concurrent callers race the same slot. Exactly one confirms;
the other 99 get alternatives. Duplicate reservations: 0."""

import threading

from receptionist.booking.slots import SlotTable

STORE = "acceptance-p0-4"
N = 100


def test_100_concurrent_same_slot_zero_duplicates(tmp_path):
    slots = SlotTable(tmp_path / "slots.db")
    slots.seed(STORE, ["fri-1900", "fri-2000", "sat-1200", "sat-1900"])

    results: list[dict] = []
    lock = threading.Lock()

    def worker(i: int):
        res = slots.reserve(STORE, "fri-1900", f"+141555{i:05d}", party_size=2)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
    assert len(losers) == N - 1

    for loser in losers:
        assert loser["alternatives"], "loser received no alternative slots"

    slot = slots.get(STORE, "fri-1900")
    assert slot["status"] == "reserved"
    assert slot["held_by"] and slot["party_size"] == 2

    # Cross-check: the table shows exactly one holder, no duplicates.
    all_slots = slots.list(STORE)
    holders = [s["held_by"] for s in all_slots if s["slot_id"] == "fri-1900"]
    assert len(holders) == 1
