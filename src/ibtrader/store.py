"""SQLite state store: raw payloads, signals, intents, positions, P&L, append-only audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    transport TEXT NOT NULL,
    producer TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    status TEXT,
    errors TEXT
);
CREATE INDEX IF NOT EXISTS raw_signals_producer_time ON raw_signals (producer, received_at);

CREATE TABLE IF NOT EXISTS signals (
    producer TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    underlying TEXT,
    strategy TEXT,
    status TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    data TEXT NOT NULL,
    body TEXT NOT NULL,
    raw_id INTEGER NOT NULL REFERENCES raw_signals (id),
    received_at TEXT NOT NULL,
    last_eval_at TEXT,
    last_eval_date TEXT,
    entries_date TEXT,
    entries_today INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (producer, signal_id, version)
);

CREATE TABLE IF NOT EXISTS signal_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    producer TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    producer TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- entry | exit
    underlying TEXT NOT NULL,
    strategy TEXT NOT NULL,
    legs TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    limit_price REAL,
    max_price REAL,                  -- entry: never pay more; exit: floor (never sell lower)
    mid REAL,
    natural REAL,
    status TEXT NOT NULL,
    rule TEXT,
    reason TEXT,
    shadow INTEGER NOT NULL DEFAULT 0,
    position_id TEXT,
    broker_order_id TEXT,
    order_ref TEXT,
    reprice_step INTEGER NOT NULL DEFAULT 0,
    max_reprice_steps INTEGER NOT NULL DEFAULT 0,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    exit_reason TEXT,
    approval_deadline TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_action_at TEXT
);
CREATE INDEX IF NOT EXISTS intents_status ON intents (status);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    producer TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    underlying TEXT NOT NULL,
    strategy TEXT NOT NULL,
    legs TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_rules TEXT NOT NULL,
    max_reprice_steps INTEGER NOT NULL DEFAULT 3,
    shadow INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,            -- open | closed
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pnl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    producer TEXT NOT NULL,
    at TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    pnl REAL NOT NULL,
    shadow INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS producer_pauses (
    producer TEXT PRIMARY KEY,
    paused_at TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    producer TEXT,
    signal_id TEXT,
    version INTEGER,
    intent_id TEXT,
    data TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

ACTIVE_SIGNAL_STATUSES = ("pending", "active")
WORKING_INTENT_STATUSES = ("submitted",)
OPEN_INTENT_STATUSES = ("awaiting_approval", "approved", "submitted")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    # -- plumbing -----------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        self._conn.close()

    # -- raw payloads -------------------------------------------------------------
    def record_raw(
        self,
        *,
        received_at: datetime,
        transport: str,
        producer: str,
        sha256: str,
        size: int,
        payload: str,
        truncated: bool,
    ) -> int:
        cur = self.execute(
            "INSERT INTO raw_signals (received_at, transport, producer, sha256, size, truncated,"
            " payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso(received_at), transport, producer, sha256, size, int(truncated), payload),
        )
        return int(cur.lastrowid)

    def set_raw_status(self, raw_id: int, status: str, errors: list[dict] | None) -> None:
        self.execute(
            "UPDATE raw_signals SET status = ?, errors = ? WHERE id = ?",
            (status, json.dumps(errors or []), raw_id),
        )

    def count_raw_since(self, producer: str, since: datetime) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM raw_signals WHERE producer = ? AND received_at >= ?",
            (producer, iso(since)),
        )
        return int(row["n"]) if row else 0

    # -- signals ------------------------------------------------------------------
    def get_signal(self, producer: str, signal_id: str, version: int | None = None):
        if version is None:
            return self.one(
                "SELECT * FROM signals WHERE producer = ? AND signal_id = ?"
                " ORDER BY version DESC LIMIT 1",
                (producer, signal_id),
            )
        return self.one(
            "SELECT * FROM signals WHERE producer = ? AND signal_id = ? AND version = ?",
            (producer, signal_id, version),
        )

    def insert_signal(
        self,
        *,
        producer: str,
        signal_id: str,
        version: int,
        kind: str,
        underlying: str | None,
        strategy: str | None,
        status: str,
        valid_until: datetime,
        data: dict[str, Any],
        body: str,
        raw_id: int,
        received_at: datetime,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO signals (producer, signal_id, version, kind, underlying, strategy,"
                " status, valid_until, data, body, raw_id, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    producer,
                    signal_id,
                    version,
                    kind,
                    underlying,
                    strategy,
                    status,
                    iso(valid_until),
                    json.dumps(data),
                    body,
                    raw_id,
                    iso(received_at),
                ),
            )
            conn.execute(
                "INSERT INTO signal_status_history (producer, signal_id, version, status, reason,"
                " at) VALUES (?, ?, ?, ?, ?, ?)",
                (producer, signal_id, version, status, "accepted", iso(received_at)),
            )

    def set_signal_status(
        self, producer: str, signal_id: str, version: int, status: str, reason: str, at: datetime
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE signals SET status = ?"
                " WHERE producer = ? AND signal_id = ? AND version = ?",
                (status, producer, signal_id, version),
            )
            conn.execute(
                "INSERT INTO signal_status_history (producer, signal_id, version, status, reason,"
                " at) VALUES (?, ?, ?, ?, ?, ?)",
                (producer, signal_id, version, status, reason, iso(at)),
            )

    def signals_with_status(self, *statuses: str, kind: str | None = None):
        marks = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM signals WHERE status IN ({marks})"  # noqa: S608 - placeholders only
        params: tuple = statuses
        if kind:
            sql += " AND kind = ?"
            params = (*statuses, kind)
        return self.query(sql + " ORDER BY received_at", params)

    def signal_history(self, producer: str, signal_id: str):
        return self.query(
            "SELECT version, status, reason, at FROM signal_status_history"
            " WHERE producer = ? AND signal_id = ? ORDER BY id",
            (producer, signal_id),
        )

    def update_rule_eval(
        self, producer: str, signal_id: str, version: int, at: datetime, eval_date: str
    ) -> None:
        self.execute(
            "UPDATE signals SET last_eval_at = ?, last_eval_date = ?"
            " WHERE producer = ? AND signal_id = ? AND version = ?",
            (iso(at), eval_date, producer, signal_id, version),
        )

    def bump_rule_entries(self, producer: str, signal_id: str, version: int, day: str) -> None:
        self.execute(
            "UPDATE signals SET entries_today = CASE WHEN entries_date = ? THEN entries_today + 1"
            " ELSE 1 END, entries_date = ? WHERE producer = ? AND signal_id = ? AND version = ?",
            (day, day, producer, signal_id, version),
        )

    # -- intents ------------------------------------------------------------------
    def insert_intent(self, now: datetime, **fields: Any) -> str:
        intent_id = fields.pop("id", None) or new_id("int")
        fields.update(id=intent_id, created_at=iso(now), updated_at=iso(now))
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        self.execute(
            f"INSERT INTO intents ({cols}) VALUES ({marks})",  # noqa: S608 - internal columns
            tuple(fields.values()),
        )
        return intent_id

    def update_intent(self, intent_id: str, now: datetime, **fields: Any) -> None:
        fields["updated_at"] = iso(now)
        assignments = ",".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE intents SET {assignments} WHERE id = ?",  # noqa: S608 - internal columns
            (*fields.values(), intent_id),
        )

    def get_intent(self, intent_id: str):
        return self.one("SELECT * FROM intents WHERE id = ?", (intent_id,))

    def intents_with_status(self, *statuses: str):
        marks = ",".join("?" * len(statuses))
        return self.query(
            f"SELECT * FROM intents WHERE status IN ({marks}) ORDER BY created_at",  # noqa: S608
            statuses,
        )

    def open_intents_for_position(self, position_id: str):
        """Open exit intents, plus `unknown` ones: an exit whose fate is unclear after a restart
        blocks further exits until reconciled, so we never sell the same position twice."""
        statuses = (*OPEN_INTENT_STATUSES, "unknown")
        marks = ",".join("?" * len(statuses))
        return self.query(
            f"SELECT * FROM intents WHERE position_id = ? AND status IN ({marks})",  # noqa: S608
            (position_id, *statuses),
        )

    def last_failed_exit_at(self, position_id: str) -> datetime | None:
        row = self.one(
            "SELECT MAX(updated_at) AS at FROM intents WHERE position_id = ? AND kind = 'exit'"
            " AND status IN ('rejected', 'error', 'cancelled') AND filled_qty = 0",
            (position_id,),
        )
        return parse_iso(row["at"]) if row and row["at"] else None

    # -- positions ----------------------------------------------------------------
    def insert_position(self, now: datetime, **fields: Any) -> str:
        position_id = new_id("pos")
        fields.update(id=position_id, opened_at=iso(now), status="open")
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        self.execute(
            f"INSERT INTO positions ({cols}) VALUES ({marks})",  # noqa: S608 - internal columns
            tuple(fields.values()),
        )
        return position_id

    def get_position(self, position_id: str):
        return self.one("SELECT * FROM positions WHERE id = ?", (position_id,))

    def open_positions(self, shadow: bool | None = None):
        if shadow is None:
            return self.query("SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at")
        return self.query(
            "SELECT * FROM positions WHERE status = 'open' AND shadow = ? ORDER BY opened_at",
            (int(shadow),),
        )

    def record_close(self, position_id: str, qty: int, exit_price: float, now: datetime) -> float:
        """Reduce/close a position by `qty` at `exit_price` (per share). Returns realised P&L."""
        with self.tx() as conn:
            pos = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            qty = min(qty, pos["quantity"])
            pnl = (exit_price - pos["entry_price"]) * 100 * qty
            remaining = pos["quantity"] - qty
            conn.execute(
                "UPDATE positions SET quantity = ?, realized_pnl = realized_pnl + ?, status = ?,"
                " closed_at = ? WHERE id = ?",
                (
                    remaining,
                    pnl,
                    "open" if remaining > 0 else "closed",
                    None if remaining > 0 else iso(now),
                    position_id,
                ),
            )
            conn.execute(
                "INSERT INTO pnl_events (position_id, producer, at, quantity, pnl, shadow)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (position_id, pos["producer"], iso(now), qty, pnl, pos["shadow"]),
            )
        return pnl

    def realized_pnl_since(self, since: datetime, shadow: bool = False) -> float:
        row = self.one(
            "SELECT COALESCE(SUM(pnl), 0) AS p FROM pnl_events WHERE at >= ? AND shadow = ?",
            (iso(since), int(shadow)),
        )
        return float(row["p"]) if row else 0.0

    # -- producer pauses / kv -----------------------------------------------------
    def pause_producer(self, producer: str, reason: str, now: datetime) -> None:
        self.execute(
            "INSERT OR REPLACE INTO producer_pauses (producer, paused_at, reason) VALUES (?, ?, ?)",
            (producer, iso(now), reason),
        )

    def resume_producer(self, producer: str) -> None:
        self.execute("DELETE FROM producer_pauses WHERE producer = ?", (producer,))

    def is_producer_paused(self, producer: str) -> bool:
        return self.one("SELECT 1 FROM producer_pauses WHERE producer = ?", (producer,)) is not None

    def get_kv(self, key: str) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # -- audit --------------------------------------------------------------------
    def append_audit(
        self,
        ts: datetime,
        event: str,
        data: dict[str, Any],
        producer: str | None = None,
        signal_id: str | None = None,
        version: int | None = None,
        intent_id: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO audit_log (ts, event, producer, signal_id, version, intent_id, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso(ts),
                event,
                producer,
                signal_id,
                version,
                intent_id,
                json.dumps(data, default=str),
            ),
        )

    # -- reporting ----------------------------------------------------------------
    def scorecard(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT p.producer,
              (SELECT COUNT(*) FROM raw_signals r WHERE r.producer = p.producer) AS received,
              (SELECT COUNT(*) FROM raw_signals r WHERE r.producer = p.producer
                 AND r.status = 'accepted') AS accepted,
              (SELECT COUNT(*) FROM raw_signals r WHERE r.producer = p.producer
                 AND r.status = 'rejected') AS rejected,
              (SELECT COUNT(*) FROM positions x WHERE x.producer = p.producer) AS trades,
              (SELECT COUNT(*) FROM positions x WHERE x.producer = p.producer
                 AND x.status = 'closed') AS closed,
              (SELECT COUNT(*) FROM positions x WHERE x.producer = p.producer
                 AND x.status = 'closed' AND x.realized_pnl > 0) AS wins,
              (SELECT COALESCE(SUM(pnl), 0) FROM pnl_events e WHERE e.producer = p.producer
                 AND e.shadow = 0) AS pnl,
              (SELECT COALESCE(SUM(pnl), 0) FROM pnl_events e WHERE e.producer = p.producer
                 AND e.shadow = 1) AS shadow_pnl
            FROM (SELECT DISTINCT producer FROM raw_signals) p ORDER BY p.producer
            """
        )
        out = []
        for row in rows:
            item = dict(row)
            item["hit_rate"] = round(item["wins"] / item["closed"], 3) if item["closed"] else None
            out.append(item)
        return out

    def since(self, now: datetime, hours: float) -> datetime:
        return now - timedelta(hours=hours)
