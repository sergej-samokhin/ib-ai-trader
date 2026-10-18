"""Session calendar: entry window, early closes, check_at parsing and New York DST."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from ibtrader import clock
from ibtrader.clock import (
    EARLY_CLOSES,
    HOLIDAYS,
    NY,
    dte,
    in_entry_window,
    is_market_open,
    is_trading_day,
    ny_date,
    parse_check_at,
    session_bounds,
    to_ny,
)


def utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------- trading days
def test_weekdays_are_trading_days_weekends_are_not():
    assert is_trading_day(date(2026, 10, 19))  # Monday
    assert is_trading_day(date(2026, 10, 23))  # Friday
    assert not is_trading_day(date(2026, 10, 17))  # Saturday
    assert not is_trading_day(date(2026, 10, 18))  # Sunday


@pytest.mark.parametrize("holiday", sorted(HOLIDAYS))
def test_holidays_are_not_trading_days(holiday):
    assert not is_trading_day(holiday)
    assert session_bounds(holiday) is None


def test_holiday_table_contains_known_dates():
    assert date(2026, 11, 26) in HOLIDAYS  # Thanksgiving
    assert date(2026, 12, 25) in HOLIDAYS
    assert date(2027, 1, 1) in HOLIDAYS


def test_early_closes_are_trading_days():
    for day in EARLY_CLOSES:
        assert is_trading_day(day)


# ---------------------------------------------------------------------------- session bounds
def test_regular_session_bounds():
    open_, close = session_bounds(date(2026, 10, 19))
    assert open_.timetz().replace(tzinfo=None) == time(9, 30)
    assert close.timetz().replace(tzinfo=None) == time(16, 0)
    assert open_.tzinfo == NY


def test_early_close_session_ends_at_one_pm():
    day = date(2026, 11, 27)
    assert day in EARLY_CLOSES
    _, close = session_bounds(day)
    assert close.hour == 13
    assert close.minute == 0


def test_market_open_boundaries():
    # 9:30 open is inclusive, 16:00 close is exclusive (EDT = UTC-4)
    assert not is_market_open(utc(2026, 10, 19, 13, 29))
    assert is_market_open(utc(2026, 10, 19, 13, 30))
    assert is_market_open(utc(2026, 10, 19, 19, 59))
    assert not is_market_open(utc(2026, 10, 19, 20, 0))


def test_market_closed_on_weekend_and_holiday():
    assert not is_market_open(utc(2026, 10, 17, 15))
    assert not is_market_open(utc(2026, 11, 26, 15))


def test_market_closes_at_one_pm_on_early_close_day():
    assert is_market_open(utc(2026, 11, 27, 17, 59))  # 12:59 EST
    assert not is_market_open(utc(2026, 11, 27, 18, 0))  # 13:00 EST


# ---------------------------------------------------------------------------- entry window
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (utc(2026, 10, 19, 13, 44), False),  # 9:44 NY
        (utc(2026, 10, 19, 13, 45), True),  # 9:45 NY
        (utc(2026, 10, 19, 14, 30), True),
        (utc(2026, 10, 19, 19, 44), True),  # 15:44 NY
        (utc(2026, 10, 19, 19, 45), False),  # 15:45 NY
        (utc(2026, 10, 19, 20, 30), False),
        (utc(2026, 10, 17, 14, 30), False),  # Saturday
        (utc(2026, 11, 26, 15, 0), False),  # Thanksgiving
    ],
)
def test_entry_window_with_fifteen_minute_buffers(moment, expected):
    assert in_entry_window(moment, 15, 15) is expected


def test_entry_window_with_different_buffers():
    assert in_entry_window(utc(2026, 10, 19, 13, 30), 0, 0)
    assert not in_entry_window(utc(2026, 10, 19, 13, 59), 30, 0)
    assert in_entry_window(utc(2026, 10, 19, 14, 0), 30, 0)
    assert not in_entry_window(utc(2026, 10, 19, 19, 29), 0, 31)
    assert in_entry_window(utc(2026, 10, 19, 19, 29), 0, 30)


def test_entry_window_on_early_close_day():
    assert in_entry_window(utc(2026, 11, 27, 17, 44), 15, 15)  # 12:44 EST
    assert not in_entry_window(utc(2026, 11, 27, 17, 45), 15, 15)  # 12:45 EST
    assert not in_entry_window(utc(2026, 11, 27, 19, 0), 15, 15)


def test_entry_window_buffers_wider_than_session_never_allow_entries():
    moment = utc(2026, 10, 19, 17, 0)
    assert not in_entry_window(moment, 240, 240)


# ---------------------------------------------------------------------------- parse_check_at
def test_parse_check_at():
    assert parse_check_at("15:45 America/New_York") == time(15, 45)
    assert parse_check_at("09:30 America/New_York") == time(9, 30)
    assert parse_check_at("00:00 America/New_York") == time(0, 0)


@pytest.mark.parametrize(
    "value", ["15:45 UTC", "15:45 Europe/Berlin", "15:45", "15:45 america/new_york"]
)
def test_parse_check_at_rejects_other_zones(value):
    with pytest.raises(ValueError, match="America/New_York"):
        parse_check_at(value)


def test_parse_check_at_rejects_garbage_time():
    with pytest.raises(ValueError):
        parse_check_at("noon America/New_York")


# ---------------------------------------------------------------------------- New York DST
def test_dst_starts_in_march():
    # DST began Sunday 2026-03-08: Friday is EST (UTC-5), Monday is EDT (UTC-4)
    friday_open, _ = session_bounds(date(2026, 3, 6))
    monday_open, _ = session_bounds(date(2026, 3, 9))
    assert friday_open.astimezone(UTC) == utc(2026, 3, 6, 14, 30)
    assert monday_open.astimezone(UTC) == utc(2026, 3, 9, 13, 30)


def test_dst_ends_in_november():
    # DST ended Sunday 2026-11-01: Friday is EDT (UTC-4), Monday is EST (UTC-5)
    friday_open, friday_close = session_bounds(date(2026, 10, 30))
    monday_open, monday_close = session_bounds(date(2026, 11, 2))
    assert friday_open.astimezone(UTC) == utc(2026, 10, 30, 13, 30)
    assert friday_close.astimezone(UTC) == utc(2026, 10, 30, 20, 0)
    assert monday_open.astimezone(UTC) == utc(2026, 11, 2, 14, 30)
    assert monday_close.astimezone(UTC) == utc(2026, 11, 2, 21, 0)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (utc(2026, 3, 6, 14, 29), False),  # 9:29 EST
        (utc(2026, 3, 6, 14, 30), True),
        (utc(2026, 3, 9, 13, 29), False),  # 9:29 EDT
        (utc(2026, 3, 9, 13, 30), True),
        (utc(2026, 11, 2, 14, 29), False),  # 9:29 EST
        (utc(2026, 11, 2, 14, 30), True),
        (utc(2026, 11, 2, 13, 30), False),  # would be open one week earlier
    ],
)
def test_market_open_follows_local_time_across_dst(moment, expected):
    assert is_market_open(moment) is expected


def test_entry_window_follows_local_time_across_dst():
    # 9:45 local both before and after the changes
    assert in_entry_window(utc(2026, 3, 6, 14, 45), 15, 15)
    assert not in_entry_window(utc(2026, 3, 6, 14, 44), 15, 15)
    assert in_entry_window(utc(2026, 3, 9, 13, 45), 15, 15)
    assert not in_entry_window(utc(2026, 3, 9, 13, 44), 15, 15)
    assert in_entry_window(utc(2026, 11, 2, 14, 45), 15, 15)
    assert not in_entry_window(utc(2026, 11, 2, 14, 44), 15, 15)


def test_ny_date_uses_new_york_calendar_day():
    late_evening = utc(2026, 10, 20, 2, 0)  # 22:00 on the 19th in New York
    assert ny_date(late_evening) == date(2026, 10, 19)
    assert ny_date(utc(2026, 10, 20, 4, 0)) == date(2026, 10, 20)  # 00:00 EDT
    # DST ended on 2026-11-01 at 06:00 UTC: midnight of the 2nd is 05:00 UTC afterwards
    assert ny_date(utc(2026, 11, 2, 4, 59)) == date(2026, 11, 1)  # 23:59 EST
    assert ny_date(utc(2026, 11, 2, 5, 0)) == date(2026, 11, 2)


def test_to_ny_converts_without_changing_the_instant():
    moment = utc(2026, 10, 19, 14, 30)
    local = to_ny(moment)
    assert local.hour == 10
    assert local == moment
    assert local.utcoffset() == timedelta(hours=-4)
    assert to_ny(utc(2026, 11, 2, 15)).utcoffset() == timedelta(hours=-5)


def test_dte_counts_calendar_days():
    assert dte(date(2026, 11, 20), date(2026, 10, 19)) == 32
    assert dte(date(2026, 10, 19), date(2026, 10, 19)) == 0
    assert dte(date(2026, 10, 18), date(2026, 10, 19)) == -1


def test_utcnow_is_timezone_aware_utc():
    now = clock.utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
