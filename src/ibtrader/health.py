"""Connection health: one alert per outage, periodic reminders, one recovery message."""

from __future__ import annotations

import logging
from datetime import datetime

from ibtrader.audit import Audit
from ibtrader.notify import Notifier


class ConnectionWatch:
    def __init__(self, notifier: Notifier, audit: Audit, reminder_minutes: float = 30.0):
        self.notifier = notifier
        self.audit = audit
        self.reminder_seconds = reminder_minutes * 60
        self.down_since: datetime | None = None
        self._last_alert: datetime | None = None
        self.failures = 0

    @property
    def is_down(self) -> bool:
        return self.down_since is not None

    def failed(self, now: datetime, error: Exception | str) -> None:
        self.failures += 1
        if self.down_since is None:
            self.down_since = self._last_alert = now
            self.audit("connection_lost", now, level=logging.ERROR, error=str(error))
            self.notifier.send(
                "IBKR connection lost",
                f"{error}\nNew entries, exits, approvals and the kill switch wait until the "
                "connection is back. Signals keep being accepted. Check IB Gateway / IBC login.",
                "critical",
            )
            return
        assert self._last_alert is not None
        if (now - self._last_alert).total_seconds() >= self.reminder_seconds:
            self._last_alert = now
            minutes = (now - self.down_since).total_seconds() / 60
            self.notifier.send(
                "IBKR still disconnected",
                f"Down for {minutes:.0f} min ({self.failures} failed attempts). "
                f"Last error: {error}",
                "critical",
            )

    def ok(self, now: datetime) -> bool:
        """Mark the connection healthy. Returns True if this ends an outage."""
        if self.down_since is None:
            return False
        minutes = (now - self.down_since).total_seconds() / 60
        self.audit(
            "connection_restored",
            now,
            level=logging.WARNING,
            down_minutes=round(minutes, 1),
            failed_attempts=self.failures,
        )
        self.notifier.send(
            "IBKR connection restored",
            f"Back after {minutes:.0f} min. Working orders are re-attached and recovery runs now.",
            "warning",
        )
        self.down_since = self._last_alert = None
        self.failures = 0
        return True
