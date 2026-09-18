"""Long-term memory store. SQLite, one file per deployment.

Three kinds of long-term memory, kept strictly separate:

- current-value slots: key/value per (store, caller). Overwrite semantics;
  always loaded into the prompt at call start. Wins any conflict with history.
- history: append-only (enforced by DB triggers). Search-only access; never
  preloaded into the prompt.
- procedural rules: created/modified ONLY through the owner-correction path.
  Every rule carries its source (correction timestamp + original text).

Raw call transcripts are never written here (see handoff.py).
"""

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS current_slots (
  store_id  TEXT NOT NULL,
  caller_id TEXT NOT NULL,
  key       TEXT NOT NULL,
  value     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (store_id, caller_id, key)
);

CREATE TABLE IF NOT EXISTS history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id   TEXT NOT NULL,
  caller_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  text       TEXT NOT NULL,
  meta       TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_caller ON history(store_id, caller_id);

CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON history
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON history
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;

CREATE TABLE IF NOT EXISTS rules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id    TEXT NOT NULL,
  rule_text   TEXT NOT NULL,
  source_text TEXT NOT NULL,
  source_at   TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """Thread-safe store; opens a short-lived connection per operation so it
    is safe to share across concurrent call workers."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def _tx(self):
        """Transaction scope that closes the connection on exit — a bare
        `with conn:` commits but never closes."""
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- current-value slots (overwrite; prompt always includes these) ----

    def set_current(self, store_id: str, caller_id: str, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO current_slots(store_id, caller_id, key, value, updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(store_id, caller_id, key) DO UPDATE SET"
                "   value=excluded.value, updated_at=excluded.updated_at",
                (store_id, caller_id, key, value, _now()),
            )

    def get_current(self, store_id: str, caller_id: str) -> dict[str, str]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT key, value FROM current_slots WHERE store_id=? AND caller_id=?",
                (store_id, caller_id),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- history (append-only; search-only reads) ----

    def append_history(self, store_id: str, caller_id: str, kind: str,
                       text: str, meta: dict | None = None) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO history(store_id, caller_id, kind, text, meta, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (store_id, caller_id, kind, text,
                 json.dumps(meta) if meta else None, _now()),
            )
            return cur.lastrowid

    def search_history(self, store_id: str, caller_id: str, query: str,
                       k: int = 3) -> list[dict]:
        terms = [t.lower() for t in query.split() if t.strip()]
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, kind, text, meta, created_at FROM history"
                " WHERE store_id=? AND caller_id=? ORDER BY id DESC",
                (store_id, caller_id),
            ).fetchall()
        scored = []
        for r in rows:
            hay = r["text"].lower()
            score = sum(1 for t in terms if t in hay)
            if score or not terms:
                scored.append((score, dict(r)))
        scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
        return [r for _, r in scored[:k]]

    def recent_by_kind(self, store_id: str, kind: str, k: int = 200) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, caller_id, kind, text, meta, created_at FROM history"
                " WHERE store_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                (store_id, kind, k),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- procedural rules (owner-correction path only) ----

    def owner_correct(self, store_id: str, rule_text: str, source_text: str,
                      supersedes: int | None = None) -> int:
        """Create a procedural rule. REQUIRES the owner's correction text as
        source — this is the only code path that writes rules."""
        if not source_text or not source_text.strip():
            raise ValueError("procedural rules require an owner-correction source")
        with self._tx() as c:
            if supersedes is not None:
                c.execute(
                    "UPDATE rules SET active=0, source_text=source_text||' | superseded: '||?"
                    " WHERE id=? AND store_id=?",
                    (source_text, supersedes, store_id),
                )
            cur = c.execute(
                "INSERT INTO rules(store_id, rule_text, source_text, source_at, active)"
                " VALUES(?,?,?,?,1)",
                (store_id, rule_text, source_text, _now()),
            )
            return cur.lastrowid

    def get_rules(self, store_id: str) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, rule_text, source_text, source_at FROM rules"
                " WHERE store_id=? AND active=1 ORDER BY id",
                (store_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- introspection ----

    def dump_all_text(self, store_id: str) -> str:
        """Every stored text field concatenated — used by tests to prove raw
        transcripts never persist."""
        parts: list[str] = []
        with self._tx() as c:
            for table, cols in (
                ("current_slots", ("key", "value")),
                ("history", ("kind", "text", "meta")),
                ("rules", ("rule_text", "source_text")),
            ):
                rows = c.execute(
                    f"SELECT * FROM {table} WHERE store_id=?", (store_id,)
                ).fetchall()
                for r in rows:
                    parts.extend(str(r[col]) for col in cols if r[col])
        return "\n".join(parts)
