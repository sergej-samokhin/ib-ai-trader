"""US equity-options session calendar (regular hours, America/New_York)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# NYSE full-day closures. VERIFY against the official NYSE calendar before each year.
HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)  # fmt: skip
EARLY_CLOSES: frozenset[date] = frozenset(
    {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_ny(moment: datetime) -> datetime:
    return moment.astimezone(NY)


def ny_date(moment: datetime) -> date:
    return to_ny(moment).date()


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    if not is_trading_day(day):
        return None
    close = EARLY_CLOSE if day in EARLY_CLOSES else SESSION_CLOSE
    return datetime.combine(day, SESSION_OPEN, NY), datetime.combine(day, close, NY)


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


def dte(expiry: date, today: date) -> int:
    return (expiry - today).days
