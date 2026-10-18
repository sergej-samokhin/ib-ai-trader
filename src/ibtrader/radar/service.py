"""Radar orchestration: one bounded scan, the daily feature log, and the dashboard snapshot.

No overlapping scans (thread lock plus a lease in the store for other processes), no hidden
demo fallback: a failed scan is recorded as an error and retried on the next interval.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from ibtrader.clock import NY, ny_date
from ibtrader.radar import features
from ibtrader.radar.analytics import analyze, count_mentions, extract_tickers
from ibtrader.radar.catalog import load_catalog
from ibtrader.radar.config import RadarSettings
from ibtrader.radar.providers import ProviderError, demo, fetch, http_request
from ibtrader.radar.store import RadarStore

log = logging.getLogger("ibtrader.radar")
DAILY_CUTOFF = time(15, 0)  # strategy section 3.3: aggregated daily at 15:00 ET
FEATURE_RETENTION_DAYS = 400
LEASE_SECONDS = 600
METHOD = (
    "Rule-based, ticker-local sentiment on a partial sample. Sarcasm and ambiguous context may "
    "be misclassified. Discussion activity, not investment advice."
)


def _daily_cutoff(moment: datetime) -> float:
    local = moment.astimezone(NY)
    return datetime.combine(local.date(), DAILY_CUTOFF, tzinfo=NY).timestamp()


class Radar:
    def __init__(
        self,
        settings: RadarSettings,
        directory: Path,
        request: Callable[..., Any] = http_request,
        store: RadarStore | None = None,
    ):
        self.settings = settings
        self.directory = directory
        self.request = request
        self.store = store or RadarStore(directory / "radar.sqlite3")
        self._lock = threading.Lock()
        self.running = False
        self.catalog: dict[str, dict] = {}
        self.catalog_time: float | None = None

    @property
    def provider(self) -> str:
        return self.settings.provider

    def _status(self) -> dict[str, Any]:
        return self.store.get_meta(f"status:{self.provider}", {}) or {}

    def _load_catalog(self, now: datetime, refresh: bool) -> None:
        self.catalog, self.catalog_time = load_catalog(
            self.directory,
            now,
            refresh=refresh and self.settings.catalog_refresh,
            request=self.request,
        )

    # -- scheduling ---------------------------------------------------------------
    def next_run(self) -> float | None:
        last = self._status().get("last_attempt")
        return (last or 0) + self.settings.interval_seconds if last else None

    def due(self, now: datetime) -> bool:
        upcoming = self.next_run()
        return upcoming is None or now.timestamp() >= upcoming

    def _acquire_lease(self, owner: str, stamp: float) -> bool:
        with self.store.tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key='scan_lease'").fetchone()
            if row and json.loads(row["value"])["expires"] > stamp:
                return False
            db.execute(
                "INSERT INTO meta VALUES ('scan_lease', ?) ON CONFLICT (key)"
                " DO UPDATE SET value=excluded.value",
                (json.dumps({"owner": owner, "expires": stamp + LEASE_SECONDS}),),
            )
        return True

    def _release_lease(self, owner: str) -> None:
        with self.store.tx() as db:
            db.execute(
                "DELETE FROM meta WHERE key='scan_lease' AND json_extract(value, '$.owner')=?",
                (owner,),
            )

    # -- scan ---------------------------------------------------------------------
    def scan(self, now: datetime) -> bool:
        """Run one bounded scan. Returns True on success, False if skipped or failed."""
        if not self._lock.acquire(blocking=False):
            return False
        owner = uuid.uuid4().hex
        start = now.timestamp()
        try:
            if not self._acquire_lease(owner, start):
                return False
            self.running = True
            try:
                return self._scan(now, start)
            finally:
                self.running = False
                self._release_lease(owner)
        finally:
            self._lock.release()

    def _scan(self, now: datetime, start: float) -> bool:
        provider = self.provider
        old = self._status()
        status = dict(old, state="collecting", last_attempt=start, error=None)
        self.store.set_meta(f"status:{provider}", status)
        try:
            self._load_catalog(now, refresh=True)
            result = fetch(self.settings, start, self.request, since=old.get("last_success"))
            stored = self.store.ingest(
                provider, result["items"], start, lambda text: extract_tickers(text, self.catalog)
            )
        except Exception as exc:  # noqa: BLE001 - every failure must become a visible status
            # ProviderError messages are sanitised; others may carry URLs or tokens.
            message = (
                str(exc)
                if isinstance(exc, ProviderError)
                else f"Collection failed: {type(exc).__name__}"
            )
            log.warning("radar scan failed", extra={"fields": {"provider": provider}})
            self.store.set_meta(f"status:{provider}", dict(status, state="error", error=message))
            self.store.record_run(provider, start, start, "error", error=message)
            return False
        coverage = result.get("coverage", {})
        self.store.set_meta(f"coverage:{provider}", coverage)
        # Growth needs two uninterrupted hours: restart the baseline clock after a gap.
        gap = start - (old.get("last_success") or 0) > self.settings.interval_seconds * 2
        if gap or not self.store.get_meta(f"began:{provider}"):
            self.store.set_meta(f"began:{provider}", start)
        state = "partial" if coverage.get("partial") else "ok"
        self.store.set_meta(
            f"status:{provider}", dict(status, state=state, last_success=start, error=None)
        )
        self.store.record_run(provider, start, start, state, stored)
        self._record_daily(now)
        cutoff_day = (ny_date(now) - timedelta(days=FEATURE_RETENTION_DAYS)).isoformat()
        self.store.prune(start - self.settings.retention_days * 86400, cutoff_day)
        return True

    def _record_daily(self, now: datetime) -> None:
        stamp = now.timestamp()
        rows = analyze(
            self.store.items(self.provider, stamp - 86400),
            stamp,
            self.store.first_seen(self.provider),
            self.catalog,
        )
        self.store.record_daily(
            self.provider, ny_date(now).isoformat(), stamp, _daily_cutoff(now), rows
        )

    # -- snapshot -----------------------------------------------------------------
    def snapshot(self, now: datetime, demo_mode: bool = False) -> dict[str, Any]:
        """Everything the dashboard's radar tab shows, as plain JSON-able data."""
        stamp = now.timestamp()
        if not self.catalog:
            self._load_catalog(now, refresh=False)
        if demo_mode:
            sample = demo(stamp)
            items = sample["items"]
            first = {t: stamp - 7200 for it in items for t in extract_tickers(it["text"])}
            coverage = sample["coverage"]
            status: dict[str, Any] = {
                "state": "demo",
                "last_attempt": stamp,
                "last_success": stamp,
                "error": None,
            }
            began: float | None = stamp - 86400
        else:
            items = self.store.items(self.provider, stamp - 2 * 86400)
            first = self.store.first_seen(self.provider)
            coverage = dict(self.store.get_meta(f"coverage:{self.provider}", {}) or {})
            status = {
                "state": "waiting",
                "last_attempt": None,
                "last_success": None,
                "error": None,
                **self._status(),
            }
            began = self.store.get_meta(f"began:{self.provider}")
        status["running"] = self.running and not demo_mode
        status["interval_seconds"] = self.settings.interval_seconds
        status["next_run"] = None if demo_mode else self.next_run()
        last_success = status.get("last_success")
        status["stale"] = bool(
            last_success and stamp - last_success > self.settings.interval_seconds * 2
        )
        baseline_ready = bool(
            began
            and stamp - began >= 7200
            and not status["stale"]
            and status.get("state") != "error"
        )
        recent = [i for i in items if float(i.get("created_utc") or 0) >= stamp - 86400]
        rows = analyze(recent, stamp, first, self.catalog, baseline_ready=baseline_ready)
        if demo_mode:
            features.enrich(rows, {}, [], None)
        else:
            covered = bool(began and stamp - began >= 2 * 86400)
            previous = (
                count_mentions(items, stamp - 2 * 86400, stamp - 86400, self.catalog)
                if covered
                else None
            )
            days = self.store.tracked_days(
                self.provider, ny_date(now).isoformat(), features.BASELINE_DAYS
            )
            features.enrich(rows, self.store.daily_mentions(self.provider, days), days, previous)
        if not baseline_ready:
            coverage.update(
                warmup=True,
                warmup_note="Building a comparison baseline: hourly growth is unavailable for "
                "the first two hours of uninterrupted collection.",
            )
        return {
            "enabled": self.settings.enabled or demo_mode,
            "mode": "demo" if demo_mode else self.provider,
            "status": status,
            "coverage": coverage,
            "metrics": {
                "items_24h": len(recent),
                "posts_24h": sum(i.get("kind") == "post" for i in recent),
                "comments_24h": sum(i.get("kind") == "comment" for i in recent),
                "tickers": len(rows),
            },
            "tickers": rows,
            "generated_at": stamp,
            "catalog_size": len(self.catalog),
            "catalog_updated": self.catalog_time,
            "method": METHOD,
        }


def run_scheduler(radar: Radar, stop: threading.Event, clock: Callable[[], datetime]) -> None:
    """Background loop for the dashboard process: scan whenever the interval has elapsed."""
    while not stop.is_set():
        now = clock()
        if radar.due(now):
            radar.scan(now)
        stop.wait(30)
