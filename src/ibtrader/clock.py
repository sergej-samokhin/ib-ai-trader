"""US equity-options session calendar (regular hours, America/New_York).

Holidays and early closes come from `pandas_market_calendars` (NYSE calendar). The calendar is
computed from rules, so it covers any year; it does not query a server at runtime. Keep the
package updated to pick up corrections (e.g. newly declared market closures).
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NY = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)
_CALENDAR = mcal.get_calendar("NYSE")


@functools.lru_cache(maxsize=32)
def _sessions(year: int) -> dict[date, tuple[datetime, datetime]]:
    """Regular sessions of one year: day -> (open, close) in New York time."""
    schedule = _CALENDAR.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    return {
        day.date(): (opened.to_pydatetime().astimezone(NY), closed.to_pydatetime().astimezone(NY))
        for day, opened, closed in zip(
            schedule.index, schedule["market_open"], schedule["market_close"], strict=True
        )
    }


def holidays(year: int) -> frozenset[date]:
    """Weekdays without a regular session (full-day closures)."""
    sessions = _sessions(year)
    first = date(year, 1, 1)
    days = (first + timedelta(days=i) for i in range((date(year + 1, 1, 1) - first).days))
    return frozenset(d for d in days if d.weekday() < 5 and d not in sessions)


def early_closes(year: int) -> frozenset[date]:
    """Sessions that close before 16:00 New York time."""
    return frozenset(d for d, (_, close) in _sessions(year).items() if close.time() < SESSION_CLOSE)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_ny(moment: datetime) -> datetime:
    return moment.astimezone(NY)


def ny_date(moment: datetime) -> date:
    return to_ny(moment).date()


def is_trading_day(day: date) -> bool:
    return day in _sessions(day.year)


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    return _sessions(day.year).get(day)


def is_market_open(moment: datetime) -> bool:
    bounds = session_bounds(ny_date(moment))
    return bounds is not None and bounds[0] <= moment < bounds[1]


def in_entry_window(moment: datetime, open_buffer_min: int, close_buffer_min: int) -> bool:
    """True if new entries are allowed: session open, outside the first/last N minutes."""
    bounds = session_bounds(ny_date(moment))
    if bounds is None:
        return False
    start = bounds[0] + timedelta(minutes=open_buffer_min)
    end = bounds[1] - timedelta(minutes=close_buffer_min)
    return start <= moment < end


def parse_check_at(value: str) -> time:
    """'15:45 America/New_York' -> time(15, 45). Only New York time is supported."""
    clock_part, _, zone = value.partition(" ")
    if zone != "America/New_York":
        raise ValueError("check_at must be in America/New_York")
    hour, minute = clock_part.split(":")
    return time(int(hour), int(minute))


def trading_days_between(start: date, end: date) -> int:
    """Sessions after `start` up to and including `end` (0 if `end` <= `start`)."""
    if end <= start:
        return 0
    return sum(
        1
        for year in range(start.year, end.year + 1)
        for day in _sessions(year)
        if start < day <= end
    )


def dte(expiry: date, today: date) -> int:
    return (expiry - today).days
