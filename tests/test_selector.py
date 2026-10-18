"""Contract selector: expiry choice, strike choice by delta, debit check."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from helpers_trading import SpyMarketData, StubMarketData, quote, spec
from ibtrader.contract.models import Strategy
from ibtrader.selector import SelectionError, choose_expiry, select

TODAY = date(2026, 10, 19)


def in_days(days: int) -> date:
    return TODAY + timedelta(days=days)


@pytest.fixture
def md():
    return SpyMarketData(spots={"NVDA": 180.0}, today=lambda: TODAY)


# ------------------------------------------------------------------------------ choose_expiry
def test_choose_expiry_closest_to_window_midpoint():
    expiries = [in_days(d) for d in (7, 25, 32, 39, 60)]
    assert choose_expiry(expiries, spec(dte_min=21, dte_max=45), TODAY) == in_days(32)


def test_choose_expiry_midpoint_moves_with_the_window():
    expiries = [in_days(d) for d in (25, 32, 39)]
    assert choose_expiry(expiries, spec(dte_min=21, dte_max=27), TODAY) == in_days(25)
    assert choose_expiry(expiries, spec(dte_min=37, dte_max=45), TODAY) == in_days(39)


def test_choose_expiry_tie_prefers_the_earlier_expiry():
    # midpoint 33: DTE 30 and 36 are equally far away
    expiries = [in_days(36), in_days(30)]
    assert choose_expiry(expiries, spec(dte_min=21, dte_max=45), TODAY) == in_days(30)


def test_choose_expiry_window_bounds_are_inclusive():
    cfg = spec(dte_min=21, dte_max=45)
    assert choose_expiry([in_days(21)], cfg, TODAY) == in_days(21)
    assert choose_expiry([in_days(45)], cfg, TODAY) == in_days(45)


def test_choose_expiry_ignores_expiries_outside_the_window():
    cfg = spec(dte_min=21, dte_max=45)
    assert choose_expiry([in_days(20), in_days(44), in_days(46)], cfg, TODAY) == in_days(44)


@pytest.mark.parametrize("expiries", [[], [in_days(5), in_days(100)], [in_days(20), in_days(46)]])
def test_choose_expiry_errors_when_none_fits(expiries):
    with pytest.raises(SelectionError, match="no expiry"):
        choose_expiry(expiries, spec(dte_min=21, dte_max=45), TODAY)


def test_choose_expiry_does_not_pick_expired_or_past_dates():
    with pytest.raises(SelectionError):
        choose_expiry([in_days(-3), TODAY], spec(dte_min=1, dte_max=45), TODAY)


# ------------------------------------------------------------------------------- select()
def test_long_call_uses_calls_only(md):
    sel = select(Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY)
    assert md.requested_rights == ["C"]
    assert [(leg.right, leg.action) for leg in sel.legs] == [("C", "BUY")]
    assert all(q.right == "C" for q in sel.quotes)
    assert abs(sel.quotes[0].delta - 0.40) <= 0.10


def test_long_put_uses_puts_only(md):
    sel = select(
        Strategy.LONG_PUT, spec(long_leg_delta=0.30, short_leg_delta=None), md, "NVDA", TODAY
    )
    assert md.requested_rights == ["P"]
    assert [(leg.right, leg.action) for leg in sel.legs] == [("P", "BUY")]
    assert sel.quotes[0].delta < 0  # puts carry negative delta, matched on |delta|
    assert abs(abs(sel.quotes[0].delta) - 0.30) <= 0.10


def test_bull_call_spread_short_leg_is_higher_strike_and_lower_delta(md):
    sel = select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)
    long_leg, short_leg = sel.legs
    assert md.requested_rights == ["C"]
    assert (long_leg.right, long_leg.action) == ("C", "BUY")
    assert (short_leg.right, short_leg.action) == ("C", "SELL")
    assert short_leg.strike > long_leg.strike  # further OTM for calls
    assert sel.quotes[1].delta < sel.quotes[0].delta
    assert long_leg.expiry == short_leg.expiry == sel.expiry


def test_bear_put_spread_short_leg_is_lower_strike_and_lower_abs_delta(md):
    sel = select(Strategy.BEAR_PUT_SPREAD, spec(), md, "NVDA", TODAY)
    long_leg, short_leg = sel.legs
    assert md.requested_rights == ["P"]
    assert (long_leg.right, long_leg.action) == ("P", "BUY")
    assert (short_leg.right, short_leg.action) == ("P", "SELL")
    assert short_leg.strike < long_leg.strike  # further OTM for puts
    assert abs(sel.quotes[1].delta) < abs(sel.quotes[0].delta)


@pytest.mark.parametrize(
    "strategy", ["long_call", "long_put", "bull_call_spread", "bear_put_spread"]
)
def test_prices_are_a_positive_debit_with_natural_at_least_mid(md, strategy):
    short = 0.20 if "spread" in strategy else None
    sel = select(Strategy(strategy), spec(short_leg_delta=short), md, "NVDA", TODAY)
    assert 0 < sel.mid <= sel.natural
    assert len(sel.legs) == len(sel.quotes) == (2 if short else 1)
    assert all(leg.con_id == q.con_id for leg, q in zip(sel.legs, sel.quotes, strict=True))


def test_select_picks_the_expiry_in_the_window(md):
    sel = select(
        Strategy.LONG_CALL, spec(dte_min=21, dte_max=45, short_leg_delta=None), md, "NVDA", TODAY
    )
    # FakeMarketData lists Fridays: DTE 25/32/39 are in the window, midpoint 33 -> DTE 32
    assert sel.expiry == in_days(32)
    assert sel.expiry.weekday() == 4


def test_select_errors_when_no_expiry_fits(md):
    with pytest.raises(SelectionError, match="no expiry"):
        select(
            Strategy.LONG_CALL,
            spec(dte_min=300, dte_max=310, short_leg_delta=None),
            md,
            "NVDA",
            TODAY,
        )


def test_delta_tolerance_error():
    # only deep ITM / far OTM strikes exist: nothing within 0.10 of the 0.40 target
    md = StubMarketData([quote(80, delta=0.90), quote(140, delta=0.10)])
    with pytest.raises(SelectionError, match="more than 0.10"):
        select(Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY)


def test_delta_tolerance_is_a_parameter():
    md = StubMarketData([quote(80, delta=0.90), quote(100, delta=0.55)])
    with pytest.raises(SelectionError):
        select(Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY)
    sel = select(
        Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY, delta_tolerance=0.20
    )
    assert sel.legs[0].strike == 100


def test_short_leg_delta_tolerance_error():
    md = StubMarketData([quote(100, delta=0.40), quote(110, delta=0.02)])
    with pytest.raises(SelectionError, match="more than"):
        select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)


def test_short_leg_must_be_further_otm_than_the_long_leg():
    # the only strike near the short delta is BELOW the long strike -> not eligible
    md = StubMarketData([quote(100, delta=0.40), quote(90, delta=0.20)])
    with pytest.raises(SelectionError):
        select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)
    puts = StubMarketData([quote(100, "P", delta=-0.40), quote(110, "P", delta=-0.20)])
    with pytest.raises(SelectionError):
        select(Strategy.BEAR_PUT_SPREAD, spec(), puts, "NVDA", TODAY)


def test_quotes_without_delta_or_market_are_unusable():
    md = StubMarketData([quote(100, delta=None), quote(105, bid=None, ask=None, delta=0.4)])
    with pytest.raises(SelectionError, match="no quotes"):
        select(Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY)


def test_quotes_of_the_wrong_right_are_filtered_out():
    md = StubMarketData([quote(100, "P", delta=-0.40)])
    with pytest.raises(SelectionError, match="no quotes"):
        select(Strategy.LONG_CALL, spec(short_leg_delta=None), md, "NVDA", TODAY)


def test_equal_delta_distance_prefers_the_lower_strike():
    # 0.5 and 0.25 are both exactly 0.125 away from 0.375 (binary-exact, no float noise)
    md = StubMarketData([quote(105, delta=0.5), quote(100, delta=0.25)])
    cfg = spec(long_leg_delta=0.375, short_leg_delta=None)
    sel = select(Strategy.LONG_CALL, cfg, md, "NVDA", TODAY, delta_tolerance=0.2)
    assert sel.legs[0].strike == 100


# ------------------------------------------------------------------------------ debit check
def test_spread_that_does_not_price_as_a_debit_is_rejected():
    # short leg is worth more than the long leg: net credit
    md = StubMarketData(
        [
            quote(100, bid=1.00, ask=1.10, delta=0.40),
            quote(110, bid=1.50, ask=1.60, delta=0.20),
        ]
    )
    with pytest.raises(SelectionError, match="debit"):
        select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)


def test_zero_cost_spread_is_rejected():
    md = StubMarketData(
        [quote(100, bid=1.00, ask=1.20, delta=0.40), quote(110, bid=1.00, ask=1.20, delta=0.20)]
    )
    with pytest.raises(SelectionError, match="debit"):
        select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)


def test_natural_is_never_below_mid_even_for_odd_quotes():
    md = StubMarketData(
        [
            quote(100, bid=2.00, ask=2.20, delta=0.40),
            quote(110, bid=0.50, ask=0.60, delta=0.20),
        ]
    )
    sel = select(Strategy.BULL_CALL_SPREAD, spec(), md, "NVDA", TODAY)
    assert sel.mid == pytest.approx(2.10 - 0.55)
    assert sel.natural == pytest.approx(2.20 - 0.50)
    assert sel.natural >= sel.mid
