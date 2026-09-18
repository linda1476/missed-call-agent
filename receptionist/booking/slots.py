"""Shared slot table — the single store of record for a shop's bookable
slots. Reservations are written ONLY via compare-and-set; the lock scope is
one slot row (never a store-wide lock, which would kill concurrent calls).

    UPDATE slots SET status='reserved', ... WHERE slot_id=? AND status='free'

Rowcount 1 -> confirmed. Rowcount 0 -> lost the race; the caller's working
memory gets 'slot gone' and alternatives are offered.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
  store_id   TEXT NOT NULL,
  slot_id    TEXT NOT NULL,
  status     TEXT NOT NULL CHECK(status IN ('free','reserved','cancelled')),
  held_by    TEXT,
  party_size INTEGER,
  booked_at  TEXT,
  PRIMARY KEY (store_id, slot_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlotTable:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def seed(self, store_id: str, slot_ids: list[str]) -> None:
        """Test/demo helper: create slots as free (idempotent)."""
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO slots(store_id, slot_id, status)"
                " VALUES(?,?,'free')",
                [(store_id, s) for s in slot_ids],
            )

    def reserve(self, store_id: str, slot_id: str, caller_id: str,
                party_size: int) -> dict:
        """Compare-and-set reservation. Returns {ok, slot_id|alternatives}."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE slots SET status='reserved', held_by=?, party_size=?,"
                " booked_at=? WHERE store_id=? AND slot_id=? AND status='free'",
                (caller_id, party_size, _now(), store_id, slot_id),
            )
            if cur.rowcount == 1:
                return {"ok": True, "slot_id": slot_id}
        return {"ok": False, "slot_id": slot_id,
                "alternatives": self.alternatives(store_id, exclude=slot_id)}

    def alternatives(self, store_id: str, exclude: str | None = None,
                     n: int = 3) -> list[str]:
        q = ("SELECT slot_id FROM slots WHERE store_id=? AND status='free'"
             " AND slot_id != COALESCE(?, '') ORDER BY slot_id LIMIT ?")
        with self._conn() as c:
            rows = c.execute(q, (store_id, exclude, n)).fetchall()
        return [r["slot_id"] for r in rows]

    def cancel(self, store_id: str, slot_id: str, caller_id: str) -> bool:
        """CAS release: only the holder (or an override path) can free it."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE slots SET status='free', held_by=NULL, party_size=NULL,"
                " booked_at=NULL WHERE store_id=? AND slot_id=? AND"
                " status='reserved' AND held_by=?",
                (store_id, slot_id, caller_id),
            )
            return cur.rowcount == 1

    def get(self, store_id: str, slot_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM slots WHERE store_id=? AND slot_id=?",
                (store_id, slot_id),
            ).fetchone()
        return dict(r) if r else None

    def list(self, store_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM slots WHERE store_id=? ORDER BY slot_id",
                (store_id,),
            ).fetchall()
        return [dict(r) for r in rows]
