"""Risk engine: one test per rule id, sizing math and approval policy (spec section 6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from helpers_trading import (
    NOW,
    entry_request,
    portfolio,
    producer,
    quote,
    risk_engine,
    selection,
)
from ibtrader import risk
from ibtrader.clock import NY, holidays
from ibtrader.killswitch import KillSwitch
from ibtrader.risk import RiskEngine


def ny(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(UTC)


@pytest.fixture
def engine(tmp_path):
    return risk_engine(tmp_path)


def test_baseline_request_is_approved(engine):
    decision = engine.check_entry(entry_request(), portfolio())
    assert decision.approved
    assert decision.rule is None
    assert decision.quantity == 2
    assert not decision.requires_approval


# ---------------------------------------------------------------------------- hard vetoes
def test_kill_switch_rejects(tmp_path):
    flag = tmp_path / "KILL"
    engine = RiskEngine(risk_engine(tmp_path).limits, KillSwitch(flag))
    KillSwitch(flag).activate("test", NOW)
    decision = engine.check_entry(entry_request(), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.KILL_SWITCH)
    flag.unlink()
    assert engine.check_entry(entry_request(), portfolio()).approved


def test_producer_paused_by_status(engine):
    decision = engine.check_entry(entry_request(producer=producer(status="paused")), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.PRODUCER_PAUSED)


def test_producer_paused_by_runtime_flag(engine):
    decision = engine.check_entry(entry_request(producer_paused=True), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.PRODUCER_PAUSED)
    assert "momentum-agent" in decision.reason


def test_strategy_not_allowed(engine):
    decision = engine.check_entry(entry_request(strategy="naked_short_call"), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.STRATEGY_NOT_ALLOWED)


@pytest.mark.parametrize("order_type", ["market", "stop", "LIMIT"])
def test_only_limit_orders(engine, order_type):
    decision = engine.check_entry(entry_request(order_type=order_type), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.ORDER_TYPE)


# ---------------------------------------------------------------------------- market hours
@pytest.mark.parametrize(
    ("moment", "allowed"),
    [
        (ny(2026, 10, 19, 9, 29), False),  # before the open
        (ny(2026, 10, 19, 9, 30), False),  # first N minutes
        (ny(2026, 10, 19, 9, 44), False),
        (ny(2026, 10, 19, 9, 45), True),  # first allowed minute
        (ny(2026, 10, 19, 15, 44), True),  # last allowed minute
        (ny(2026, 10, 19, 15, 45), False),  # last N minutes
        (ny(2026, 10, 19, 16, 0), False),  # closed
        (ny(2026, 10, 19, 20, 0), False),
    ],
)
def test_market_hours_first_and_last_n_minutes(engine, moment, allowed):
    decision = engine.check_entry(entry_request(now=moment), portfolio())
    assert decision.approved is allowed
    if not allowed:
        assert decision.rule == risk.MARKET_HOURS


def test_market_hours_buffers_are_configurable(tmp_path):
    engine = risk_engine(tmp_path, no_entry_open_minutes=0, no_entry_close_minutes=0)
    assert engine.check_entry(entry_request(now=ny(2026, 10, 19, 9, 30)), portfolio()).approved
    assert engine.check_entry(entry_request(now=ny(2026, 10, 19, 15, 59)), portfolio()).approved
    assert not engine.check_entry(entry_request(now=ny(2026, 10, 19, 16, 0)), portfolio()).approved


def test_market_hours_rejects_weekend(engine):
    saturday = ny(2026, 10, 17, 10, 30)
    assert saturday.astimezone(NY).weekday() == 5
    decision = engine.check_entry(entry_request(now=saturday), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.MARKET_HOURS)


def test_market_hours_rejects_a_holiday(engine):
    thanksgiving = date(2026, 11, 26)
    assert thanksgiving in holidays(2026)
    moment = datetime.combine(thanksgiving, time(10, 30), NY)
    decision = engine.check_entry(entry_request(now=moment), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.MARKET_HOURS)


@pytest.mark.parametrize("holiday", sorted(holidays(2026) | holidays(2027)))
def test_every_listed_holiday_blocks_entries(engine, holiday):
    moment = datetime.combine(holiday, time(11, 0), NY)
    assert engine.check_entry(entry_request(now=moment), portfolio()).rule == risk.MARKET_HOURS


def test_market_hours_honours_early_close(engine):
    # Friday after Thanksgiving 2026 closes at 13:00 New York: entries end at 12:45.
    assert engine.check_entry(entry_request(now=ny(2026, 11, 27, 12, 44)), portfolio()).approved
    decision = engine.check_entry(entry_request(now=ny(2026, 11, 27, 12, 45)), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.MARKET_HOURS)


# ---------------------------------------------------------------------------- account limits
def test_daily_loss_limit(engine):
    decision = engine.check_entry(entry_request(), portfolio(realized_pnl_today=-300.0))
    assert (decision.approved, decision.rule) == (False, risk.DAILY_LOSS)
    assert engine.check_entry(entry_request(), portfolio(realized_pnl_today=-299.99)).approved
    assert engine.check_entry(entry_request(), portfolio(realized_pnl_today=500.0)).approved


def test_max_open_positions(engine):
    decision = engine.check_entry(entry_request(), portfolio(open_positions=5))
    assert (decision.approved, decision.rule) == (False, risk.MAX_OPEN_POSITIONS)
    assert engine.check_entry(entry_request(), portfolio(open_positions=4)).approved


# ---------------------------------------------------------------------------- liquidity
def test_liquidity_open_interest_below_minimum(engine):
    sel = selection(quote(open_interest=99))
    decision = engine.check_entry(entry_request(selection=sel), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.LIQUIDITY_OI)


def test_liquidity_open_interest_at_minimum_is_fine(engine):
    sel = selection(quote(open_interest=100))
    assert engine.check_entry(entry_request(selection=sel), portfolio()).approved


def test_liquidity_unknown_open_interest_fails_closed(engine):
    sel = selection(quote(open_interest=None))
    decision = engine.check_entry(entry_request(selection=sel), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.LIQUIDITY_OI)


def test_liquidity_checks_every_leg(engine):
    sel = selection(quote(100, open_interest=5000), quote(110, open_interest=10, delta=0.2))
    decision = engine.check_entry(
        entry_request(selection=sel, strategy="bull_call_spread"), portfolio()
    )
    assert (decision.approved, decision.rule) == (False, risk.LIQUIDITY_OI)
    assert "110" in decision.reason


def test_liquidity_spread_too_wide(engine):
    sel = selection(quote(bid=1.00, ask=1.20))  # 18% of mid
    decision = engine.check_entry(entry_request(selection=sel), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.LIQUIDITY_SPREAD)


def test_liquidity_spread_missing_quote_fails_closed(engine):
    bad = quote(bid=None, ask=None)
    sel = type(selection())(
        legs=selection().legs, quotes=[bad], expiry=bad.expiry, mid=1.0, natural=1.1
    )
    decision = engine.check_entry(entry_request(selection=sel), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.LIQUIDITY_SPREAD)


def test_liquidity_spread_limit_is_configurable(tmp_path):
    sel = selection(quote(bid=1.00, ask=1.20))
    engine = risk_engine(tmp_path, max_bid_ask_spread_pct=20.0)
    assert engine.check_entry(entry_request(selection=sel), portfolio()).approved


# ---------------------------------------------------------------------------- money caps
def test_max_position_premium_binds_when_signal_asks_less(engine):
    decision = engine.check_entry(entry_request(max_premium_usd=50.0), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.MAX_POSITION)


def test_max_portfolio_risk(engine):
    decision = engine.check_entry(entry_request(), portfolio(premium_at_risk=1950.0))
    assert (decision.approved, decision.rule) == (False, risk.MAX_PORTFOLIO)


def test_producer_budget(engine):
    pf = portfolio(by_producer={"momentum-agent": 950.0})
    decision = engine.check_entry(entry_request(), pf)
    assert (decision.approved, decision.rule) == (False, risk.PRODUCER_BUDGET)
    # Another producer's exposure does not count against this budget.
    other = portfolio(by_producer={"macro-agent": 950.0})
    assert engine.check_entry(entry_request(), other).approved


def test_zero_budget_producer_cannot_enter(engine):
    decision = engine.check_entry(entry_request(producer=producer(budget_usd=0.0)), portfolio())
    assert (decision.approved, decision.rule) == (False, risk.PRODUCER_BUDGET)


def test_underlying_concentration(engine):
    pf = portfolio(by_underlying={"NVDA": 960.0})
    decision = engine.check_entry(entry_request(), pf)
    assert (decision.approved, decision.rule) == (False, risk.UNDERLYING)
    assert engine.check_entry(entry_request(), portfolio(by_underlying={"AMD": 960.0})).approved


def test_rejection_reason_names_the_available_amount(engine):
    decision = engine.check_entry(entry_request(), portfolio(premium_at_risk=1950.0))
    assert "50.00" in decision.reason
    assert decision.quantity == 0


# ---------------------------------------------------------------------------- sizing
@pytest.mark.parametrize(
    ("max_premium", "max_contracts", "expected"),
    [
        (300.0, 5, 2),  # money binds: floor(300 / 110)
        (300.0, 1, 1),  # contract count binds
        (111.0, 9, 1),  # just over one contract of money
        (219.99, 9, 1),
        (221.0, 9, 2),
        (500.0, 100, 4),  # global MAX_POSITION_USD (500) binds: floor(500 / 110)
    ],
)
def test_quantity_is_min_of_contract_cap_and_money_cap(
    engine, max_premium, max_contracts, expected
):
    request = entry_request(max_premium_usd=max_premium, max_contracts=max_contracts)
    decision = engine.check_entry(request, portfolio())
    assert decision.approved
    assert decision.quantity == expected


def test_signal_request_can_never_exceed_global_position_cap(engine):
    request = entry_request(max_premium_usd=100_000.0, max_contracts=100)
    decision = engine.check_entry(request, portfolio())
    assert decision.approved
    assert decision.quantity == 4
    assert decision.quantity * decision.max_price * 100 <= engine.limits.max_position_usd
    # Worst case (everything at natural) is within the cap as well.
    assert decision.quantity * request.selection.natural * 100 <= engine.limits.max_position_usd


def test_global_cap_wins_even_when_it_is_lowered(tmp_path):
    engine = risk_engine(tmp_path, max_position_usd=120.0)
    decision = engine.check_entry(
        entry_request(max_premium_usd=100_000.0, max_contracts=100), portfolio()
    )
    assert decision.quantity == 1


def test_remaining_portfolio_room_limits_quantity(engine):
    decision = engine.check_entry(
        entry_request(max_premium_usd=500.0, max_contracts=10), portfolio(premium_at_risk=1700.0)
    )
    assert decision.quantity == 2  # 300 USD of room -> 2 contracts of 110


@pytest.mark.parametrize(("bid", "ask"), [(1.00, 1.10), (0.50, 0.54), (2.00, 2.15), (4.90, 5.10)])
def test_max_price_never_exceeds_natural(engine, bid, ask):
    sel = selection(quote(bid=bid, ask=ask))
    decision = engine.check_entry(
        entry_request(selection=sel, max_premium_usd=450.0, max_contracts=3), portfolio()
    )
    if decision.approved:
        assert decision.max_price <= sel.natural + 1e-12
        assert decision.limit_price <= decision.max_price + 1e-12
        assert decision.limit_price == pytest.approx(sel.mid)
        assert decision.quantity * decision.max_price * 100 <= 450.0 + 1e-9


def test_spread_is_sized_on_the_net_natural_debit(engine):
    sel = selection(
        quote(100, bid=3.00, ask=3.20, delta=0.5), quote(110, bid=1.00, ask=1.10, delta=0.3)
    )
    assert sel.natural == pytest.approx(3.20 - 1.00)
    decision = engine.check_entry(
        entry_request(selection=sel, strategy="bull_call_spread", max_premium_usd=450.0),
        portfolio(),
    )
    assert decision.approved
    assert decision.quantity == 2  # floor(450 / 220) = 2
    assert decision.max_price == pytest.approx(2.20)


# ---------------------------------------------------------------------------- approval policy
def test_manual_producer_requires_approval(engine):
    decision = engine.check_entry(entry_request(producer=producer(status="manual")), portfolio())
    assert decision.approved
    assert decision.requires_approval
    assert any("manual" in reason for reason in decision.approval_reasons)


def test_auto_producer_small_premium_needs_no_approval(engine):
    decision = engine.check_entry(entry_request(), portfolio())
    assert not decision.requires_approval
    assert decision.approval_reasons == ()


def test_premium_above_threshold_requires_approval(engine):
    # 4 contracts x 1.10 x 100 = 440 USD > 400 USD threshold
    decision = engine.check_entry(
        entry_request(max_premium_usd=500.0, max_contracts=4), portfolio()
    )
    assert decision.quantity == 4
    assert decision.requires_approval
    assert any("premium" in reason for reason in decision.approval_reasons)


def test_premium_just_below_threshold_does_not_require_approval(tmp_path):
    engine = risk_engine(tmp_path, manual_approval_above_usd=221.0)
    decision = engine.check_entry(entry_request(), portfolio())  # 2 x 110 = 220
    assert not decision.requires_approval
    tighter = risk_engine(tmp_path, manual_approval_above_usd=219.0)
    assert tighter.check_entry(entry_request(), portfolio()).requires_approval


def test_low_confidence_requires_approval(tmp_path):
    engine = risk_engine(tmp_path, min_confidence_auto=0.7)
    decision = engine.check_entry(entry_request(confidence=0.69), portfolio())
    assert decision.requires_approval
    assert any("confidence" in reason for reason in decision.approval_reasons)
    assert not engine.check_entry(entry_request(confidence=0.7), portfolio()).requires_approval


def test_all_approval_reasons_are_collected(tmp_path):
    engine = risk_engine(tmp_path, min_confidence_auto=0.9, manual_approval_above_usd=100.0)
    decision = engine.check_entry(entry_request(producer=producer(status="manual")), portfolio())
    assert len(decision.approval_reasons) == 3


def test_shadow_requests_never_require_approval(tmp_path):
    engine = risk_engine(tmp_path, min_confidence_auto=0.9, manual_approval_above_usd=100.0)
    request = entry_request(producer=producer(status="manual"), confidence=0.1, shadow=True)
    decision = engine.check_entry(request, portfolio())
    assert decision.approved
    assert not decision.requires_approval
    assert decision.approval_reasons == ()


def test_shadow_requests_are_still_risk_checked(engine):
    decision = engine.check_entry(entry_request(shadow=True), portfolio(open_positions=5))
    assert (decision.approved, decision.rule) == (False, risk.MAX_OPEN_POSITIONS)


# ---------------------------------------------------------------------------- exits
@pytest.mark.parametrize(
    ("position_qty", "action", "reduce_pct", "expected"),
    [
        (5, "close", 100, 5),
        (5, "close", 50, 5),  # close ignores the percentage
        (4, "reduce", 50, 2),
        (5, "reduce", 50, 2),  # rounds down
        (1, "reduce", 50, 1),  # never less than one contract
        (3, "reduce", 10, 1),
        (10, "reduce", 100, 10),
        (10, "reduce", 99, 9),
    ],
)
def test_exit_quantity(position_qty, action, reduce_pct, expected):
    assert RiskEngine.exit_quantity(position_qty, action, reduce_pct) == expected


def test_exit_quantity_needs_no_engine_state_and_ignores_kill_switch(tmp_path):
    flag = tmp_path / "KILL"
    KillSwitch(flag).activate("x", NOW + timedelta(0))
    assert RiskEngine.exit_quantity(3, "close", 100) == 3


def test_direction_of():
    assert risk.direction_of("long_call") == "bullish"
    assert risk.direction_of("bear_put_spread") == "bearish"
