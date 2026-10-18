"""Radar SQLite store (``var/radar/radar.sqlite3``), separate from the trading database.

Raw items are kept per provider for ``retention_days``; ``daily_features`` keeps one row per
New York day and ticker for the 30-day attention baseline (strategy section 3.3).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    provider TEXT NOT NULL,
    id TEXT NOT NULL,
    created REAL NOT NULL,
    observed REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (provider, id)
);
CREATE INDEX IF NOT EXISTS items_created ON items (provider, created);

CREATE TABLE IF NOT EXISTS seen (
    provider TEXT NOT NULL,
    ticker TEXT NOT NULL,
    first_seen REAL NOT NULL,
    PRIMARY KEY (provider, ticker)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL NOT NULL,
    state TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS daily_features (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,               -- New York date, YYYY-MM-DD
    ticker TEXT NOT NULL,
    observed REAL NOT NULL,          -- epoch of the snapshot the row comes from
    mentions_24h INTEGER NOT NULL,
    unique_authors INTEGER,
    sentiment REAL NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (provider, day, ticker)
);

CREATE TABLE IF NOT EXISTS tracked_days (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,
    observed REAL NOT NULL,
    PRIMARY KEY (provider, day)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class RadarStore:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

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

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- items --------------------------------------------------------------------
    def ingest(
        self,
        provider: str,
        items: Iterable[dict],
        now: float,
        tickers: Callable[[str], list[str]],
    ) -> int:
        """Upsert items; a deleted item is removed. Returns the number of stored items."""
        stored = 0
        with self.tx() as db:
            for item in items:
                if not item.get("id"):
                    continue
                text = f"{item.get('title') or ''} {item.get('text') or ''}".strip()
                if item.get("deleted") or text.lower() in ("[deleted]", "[removed]"):
                    db.execute(
                        "DELETE FROM items WHERE provider=? AND id=?", (provider, item["id"])
                    )
                    continue
                created = float(item.get("created_utc") or 0)
                if not 0 < created <= now + 60:
                    continue
                db.execute(
                    "INSERT INTO items VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, id) DO UPDATE"
                    " SET created=excluded.created, observed=excluded.observed,"
                    " payload=excluded.payload",
                    (provider, item["id"], created, now, json.dumps(item, ensure_ascii=False)),
                )
                stored += 1
                for ticker in tickers(text):
                    db.execute(
                        "INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (provider, ticker, now)
                    )
        return stored

    def items(self, provider: str, since: float) -> list[dict]:
        rows = self.query(
            "SELECT payload FROM items WHERE provider=? AND created>=?", (provider, since)
        )
        return [json.loads(r["payload"]) for r in rows]

    def first_seen(self, provider: str) -> dict[str, float]:
        rows = self.query("SELECT ticker, first_seen FROM seen WHERE provider=?", (provider,))
        return {r["ticker"]: r["first_seen"] for r in rows}

    def prune(self, item_cutoff: float, feature_cutoff_day: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM items WHERE created<?", (item_cutoff,))
            db.execute("DELETE FROM runs WHERE finished<?", (item_cutoff,))
            db.execute("DELETE FROM daily_features WHERE day<?", (feature_cutoff_day,))
            db.execute("DELETE FROM tracked_days WHERE day<?", (feature_cutoff_day,))

    # -- runs and status ----------------------------------------------------------
    def record_run(
        self,
        provider: str,
        started: float,
        finished: float,
        state: str,
        count: int = 0,
        error: str | None = None,
    ) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO runs (provider, started, finished, state, item_count, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (provider, started, finished, state, count, error),
            )

    def recent_runs(self, provider: str, limit: int = 10) -> list[dict]:
        rows = self.query(
            "SELECT started, finished, state, item_count, error FROM runs WHERE provider=?"
            " ORDER BY id DESC LIMIT ?",
            (provider, limit),
        )
        return [dict(r) for r in rows]

    def set_meta(self, key: str, value: Any) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO meta VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    # -- daily features -----------------------------------------------------------
    def record_daily(
        self, provider: str, day: str, observed: float, cutoff: float, rows: list[dict]
    ) -> bool:
        """Store one snapshot as the day's features, preferring the last one before ``cutoff``.

        Rule: the stored snapshot is replaced unless it was taken at or before the cutoff
        (15:00 New York) and the new one after it. Returns True if the snapshot was stored.
        """
        with self.tx() as db:
            current = db.execute(
                "SELECT observed FROM tracked_days WHERE provider=? AND day=?", (provider, day)
            ).fetchone()
            if current and current["observed"] <= cutoff < observed:
                return False
            db.execute("DELETE FROM daily_features WHERE provider=? AND day=?", (provider, day))
            db.execute(
                "INSERT INTO tracked_days VALUES (?, ?, ?) ON CONFLICT (provider, day)"
                " DO UPDATE SET observed=excluded.observed",
                (provider, day, observed),
            )
            db.executemany(
                "INSERT INTO daily_features VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        provider,
                        day,
                        r["ticker"],
                        observed,
                        r["mentions_24h"],
                        r.get("unique_authors"),
                        r["sentiment"],
                        r["rank"],
                    )
                    for r in rows
                ],
            )
        return True

    def tracked_days(self, provider: str, before_day: str, limit: int) -> list[str]:
        rows = self.query(
            "SELECT day FROM tracked_days WHERE provider=? AND day<? ORDER BY day DESC LIMIT ?",
            (provider, before_day, limit),
        )
        return [r["day"] for r in rows]

    def daily_mentions(self, provider: str, days: list[str]) -> dict[str, dict[str, int]]:
        """ticker -> {day: mentions_24h} for the given days (absent = 0 mentions)."""
        if not days:
            return {}
        marks = ",".join("?" * len(days))
        rows = self.query(
            f"SELECT ticker, day, mentions_24h FROM daily_features"  # noqa: S608 - placeholders
            f" WHERE provider=? AND day IN ({marks})",
            (provider, *days),
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["ticker"], {})[r["day"]] = r["mentions_24h"]
        return out

    def feature_history(self, provider: str, ticker: str, limit: int = 30) -> list[dict]:
        rows = self.query(
            "SELECT t.day, COALESCE(f.mentions_24h, 0) AS mentions_24h, f.unique_authors,"
            " f.sentiment, f.rank FROM tracked_days t LEFT JOIN daily_features f"
            " ON f.provider=t.provider AND f.day=t.day AND f.ticker=?"
            " WHERE t.provider=? ORDER BY t.day DESC LIMIT ?",
            (ticker, provider, limit),
        )
        return [dict(r) for r in reversed(rows)]
