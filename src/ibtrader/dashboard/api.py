"""JSON payloads of the dashboard. The trading database is opened read-only.

The dashboard never writes trading state: approvals, kill switch and manual corrections stay
with the audited CLI commands.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any

from ibtrader.clock import NY, ny_date
from ibtrader.config import Settings
from ibtrader.killswitch import KillSwitch
from ibtrader.store import OPEN_INTENT_STATUSES, Store, iso

AUDIT_LIMITS = (100, 250, 500)


class ReadOnlyStore(Store):
    """The trading Store over a ``mode=ro`` connection: reuses its queries, cannot write."""

    def __init__(self, path: Path):  # noqa: D107 - deliberately skips Store.__init__ (schema DDL)
        self._conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()


def open_trading_store(settings: Settings) -> ReadOnlyStore | None:
    return ReadOnlyStore(settings.db_path) if settings.db_path.is_file() else None


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def _bool(row: dict, key: str) -> None:
    if key in row:
        row[key] = bool(row[key])


def overview(settings: Settings, store: Store | None, now: datetime) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": iso(now),
        "mode": settings.mode,
        "kill_switch": KillSwitch(settings.kill_switch_file).is_active(),
        "db_available": store is not None,
        "pnl": {"today": 0.0, "month": 0.0, "today_shadow": 0.0, "month_shadow": 0.0},
        "open_positions": [],
        "open_intents": [],
        "active_signals": [],
        "paused_producers": [],
        "scorecard": [],
    }
    if store is None:
        return payload
    today = ny_date(now)
    midnight = datetime.combine(today, time(0), NY)
    month_start = datetime.combine(today.replace(day=1), time(0), NY)
    payload["pnl"] = {
        "today": store.realized_pnl_since(midnight),
        "month": store.realized_pnl_since(month_start),
        "today_shadow": store.realized_pnl_since(midnight, shadow=True),
        "month_shadow": store.realized_pnl_since(month_start, shadow=True),
    }
    positions = []
    for row in store.open_positions():
        item = {
            k: row[k]
            for k in (
                "id",
                "producer",
                "signal_id",
                "version",
                "underlying",
                "strategy",
                "quantity",
                "entry_price",
                "opened_at",
                "shadow",
            )
        }
        item["legs"] = _json(row["legs"]) or []
        _bool(item, "shadow")
        positions.append(item)
    payload["open_positions"] = positions
    intents = []
    for row in store.intents_with_status(*OPEN_INTENT_STATUSES, "unknown"):
        item = {
            k: row[k]
            for k in (
                "id",
                "producer",
                "signal_id",
                "version",
                "kind",
                "underlying",
                "strategy",
                "quantity",
                "limit_price",
                "max_price",
                "status",
                "reason",
                "shadow",
                "created_at",
                "approval_deadline",
            )
        }
        _bool(item, "shadow")
        intents.append(item)
    payload["open_intents"] = intents
    payload["active_signals"] = [
        {
            k: row[k]
            for k in (
                "producer",
                "signal_id",
                "version",
                "kind",
                "underlying",
                "strategy",
                "status",
                "valid_until",
                "received_at",
            )
        }
        for row in store.signals_with_status("pending", "active")
    ]
    payload["paused_producers"] = [
        dict(r) for r in store.query("SELECT producer, paused_at, reason FROM producer_pauses")
    ]
    payload["scorecard"] = store.scorecard()
    return payload


def audit(store: Store | None, limit: int) -> dict[str, Any]:
    limit = limit if limit in AUDIT_LIMITS else AUDIT_LIMITS[0]
    if store is None:
        return {"events": []}
    rows = store.query(
        "SELECT id, ts, event, producer, signal_id, version, intent_id, data FROM audit_log"
        " ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    events = []
    for row in rows:
        item = dict(row)
        item["data"] = _json(item["data"]) or {}
        events.append(item)
    return {"events": events}
