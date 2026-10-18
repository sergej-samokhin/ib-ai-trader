"""Order manager: tick rounding, repricing ladder, cancels, exits, partial fills, errors."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from helpers_trading import NOW, FailingBroker, spec
from ibtrader.audit import Audit
from ibtrader.contract.models import Strategy
from ibtrader.domain import close_prices, legs_to_json, net_prices
from ibtrader.notify import LogNotifier
from ibtrader.orders import (
    OrderManager,
    ladder,
    order_ref,
    round_buy,
    round_sell,
)
from ibtrader.selector import select
from ibtrader.sim import FakeMarketData, SimBroker
from ibtrader.store import Store

TODAY = date(2026, 10, 19)
TICK = 0.05
INTERVAL = 20.0


# ------------------------------------------------------------------------------ pure helpers
@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1.07, 1.05),
        (1.05, 1.05),
        (1.049, 1.00),
        (1.10, 1.10),
        (2.74, 2.70),
        (0.30, 0.30),  # 0.30 / 0.05 = 5.999... must not drop a tick
        (0.01, 0.05),  # never below one tick
        (0.0, 0.05),
    ],
)
def test_round_buy_rounds_down_to_the_tick(price, expected):
    assert round_buy(price, TICK) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1.07, 1.10),
        (1.05, 1.05),
        (1.051, 1.10),
        (2.51, 2.55),
        (0.30, 0.30),
        (0.01, 0.05),
        (0.0, 0.05),
    ],
)
def test_round_sell_rounds_up_to_the_tick(price, expected):
    assert round_sell(price, TICK) == pytest.approx(expected)


def test_on_tick_prices_are_unchanged():
    for ticks in range(1, 400):
        price = round(ticks * TICK, 2)
        assert round_buy(price, TICK) == pytest.approx(price), price
        assert round_sell(price, TICK) == pytest.approx(price), price


def test_buy_never_pays_more_and_sell_never_gets_less():
    for cents in range(1, 500):
        price = cents / 100
        assert round_buy(price, TICK) <= price + 1e-9 or price < TICK
        assert round_sell(price, TICK) >= price - 1e-9


def test_dime_tick():
    assert round_buy(1.07, 0.10) == pytest.approx(1.00)
    assert round_sell(1.01, 0.10) == pytest.approx(1.10)


def test_ladder_walks_linearly_from_mid_to_natural():
    assert ladder(2.0, 3.0, 0, 4) == pytest.approx(2.0)
    assert ladder(2.0, 3.0, 1, 4) == pytest.approx(2.25)
    assert ladder(2.0, 3.0, 2, 4) == pytest.approx(2.5)
    assert ladder(2.0, 3.0, 4, 4) == pytest.approx(3.0)


def test_ladder_is_capped_at_natural_and_handles_zero_steps():
    assert ladder(2.0, 3.0, 9, 4) == pytest.approx(3.0)
    assert ladder(2.0, 3.0, 3, 0) == 2.0


def test_ladder_walks_down_for_exits():
    # closing credit: natural is below mid
    assert ladder(3.0, 2.0, 2, 4) == pytest.approx(2.5)


def test_ladder_is_monotonic():
    prices = [ladder(1.0, 2.0, step, 5) for step in range(0, 8)]
    assert prices == sorted(prices)


def test_order_ref_carries_producer_signal_version_and_intent():
    ref = order_ref("momentum-agent", "nvda-momo-001", 2, "int_abc123")
    assert ref == "momentum-agent|nvda-momo-001|v2|int_abc123"
    for part in ("momentum-agent", "nvda-momo-001", "v2", "int_abc123"):
        assert part in ref


# ------------------------------------------------------------------------------- fixtures
class Rig:
    def __init__(self, fill_mode: str = "never", broker=None):
        self.md = FakeMarketData(spots={"NVDA": 180.0}, today=lambda: TODAY)
        self.store = Store(":memory:")
        self.broker = broker or SimBroker(self.md, fill_mode)
        self.shadow = SimBroker(self.md, fill_mode)
        self.notifier = LogNotifier()
        self.audit = Audit(self.store)
        self.manager = OrderManager(
            self.store,
            self.md,
            {False: self.broker, True: self.shadow},
            self.audit,
            self.notifier,
            TICK,
            INTERVAL,
        )
        self.selection = select(Strategy.BULL_CALL_SPREAD, spec(), self.md, "NVDA", TODAY)

    def entry_intent(self, *, quantity=1, max_price=None, steps=3, limit=None, shadow=0):
        sel = self.selection
        intent_id = self.store.insert_intent(
            NOW,
            producer="momentum-agent",
            signal_id="nvda-momo-001",
            version=1,
            kind="entry",
            underlying="NVDA",
            strategy="bull_call_spread",
            legs=legs_to_json(sel.legs),
            quantity=quantity,
            limit_price=sel.mid if limit is None else limit,
            max_price=sel.natural if max_price is None else max_price,
            mid=sel.mid,
            natural=sel.natural,
            status="approved",
            shadow=shadow,
            max_reprice_steps=steps,
        )
        return self.store.get_intent(intent_id)

    def exit_intent(self, *, quantity=1, steps=3):
        legs = self.selection.legs
        mid, natural = close_prices(legs, self.md.quote_legs(legs))
        intent_id = self.store.insert_intent(
            NOW,
            producer="momentum-agent",
            signal_id="nvda-momo-001",
            version=1,
            kind="exit",
            underlying="NVDA",
            strategy="bull_call_spread",
            legs=legs_to_json(legs),
            quantity=quantity,
            limit_price=mid,
            max_price=None,
            mid=mid,
            natural=natural,
            status="approved",
            max_reprice_steps=steps,
            exit_reason="take_profit",
        )
        return self.store.get_intent(intent_id)

    def refresh(self, intent):
        return self.store.get_intent(intent["id"])

    def step(self, intent, seconds: float):
        """Advance one working order at NOW + seconds with a fresh row."""
        return self.manager.step(self.refresh(intent), NOW + timedelta(seconds=seconds))

    def order_of(self, intent):
        return self.broker.orders[self.refresh(intent)["broker_order_id"]]


@pytest.fixture
def rig():
    return Rig()


# ------------------------------------------------------------------------------- submission
def test_submit_places_one_combo_order_with_reference(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    row = rig.refresh(intent)
    assert row["status"] == "submitted"
    assert row["order_ref"] == f"momentum-agent|nvda-momo-001|v1|{intent['id']}"
    order = rig.broker.orders[row["broker_order_id"]]
    assert len(rig.broker.orders) == 1
    assert [leg.action for leg in order.legs] == ["BUY", "SELL"]
    assert order.action == "BUY"
    assert order.order_ref == row["order_ref"]
    assert order.quantity == 1


def test_entry_limit_is_rounded_down_to_the_tick(rig):
    intent = rig.entry_intent(limit=2.538)
    rig.manager.submit(intent, NOW)
    assert rig.order_of(intent).limit_price == pytest.approx(2.50)


def test_exit_is_submitted_as_a_sell_rounded_up(rig):
    intent = rig.exit_intent()
    rig.manager.submit(intent, NOW)
    order = rig.order_of(intent)
    assert order.action == "SELL"
    assert order.limit_price >= intent["limit_price"] - 1e-9
    assert (order.limit_price / TICK) == pytest.approx(round(order.limit_price / TICK))


def test_shadow_intent_goes_to_the_shadow_broker_only(rig):
    intent = rig.entry_intent(shadow=1)
    rig.manager.submit(intent, NOW)
    assert rig.broker.orders == {}
    assert len(rig.shadow.orders) == 1


def test_submit_is_audited_with_identifiers(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    (event,) = rig.store.query("SELECT * FROM audit_log WHERE event = 'order_submitted'")
    assert (event["producer"], event["signal_id"], event["version"]) == (
        "momentum-agent",
        "nvda-momo-001",
        1,
    )
    assert event["intent_id"] == intent["id"]


# ----------------------------------------------------------------------------- repricing
def drive(rig, intent, steps=12, seconds=INTERVAL):
    """Step an entry every `seconds`; returns the broker price history."""
    for i in range(1, steps + 1):
        rig.step(intent, i * seconds)
    return rig.order_of(intent).history


def test_reprice_ladder_steps_up_toward_natural(rig):
    intent = rig.entry_intent(steps=3)
    rig.manager.submit(intent, NOW)
    history = drive(rig, intent, steps=3)
    assert len(history) == 4  # initial + three reprices
    assert history == sorted(history)
    assert history[-1] > history[0]
    assert history[-1] <= rig.selection.natural + 1e-9
    assert history[-1] == pytest.approx(round_buy(rig.selection.natural, TICK))
    assert rig.refresh(intent)["reprice_step"] == 3


def test_no_reprice_before_the_interval_elapsed(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    assert rig.step(intent, INTERVAL - 1) is None
    assert len(rig.order_of(intent).history) == 1
    assert rig.refresh(intent)["reprice_step"] == 0
    rig.step(intent, INTERVAL)
    assert rig.refresh(intent)["reprice_step"] == 1


def test_reprice_never_goes_above_max_price(rig):
    max_price = round(rig.selection.mid + 0.07, 2)
    intent = rig.entry_intent(max_price=max_price, steps=3)
    rig.manager.submit(intent, NOW)
    history = drive(rig, intent, steps=3)
    assert max(history) <= max_price + 1e-9
    assert history[-1] == pytest.approx(round_buy(max_price, TICK))


def test_reprice_events_are_audited(rig):
    intent = rig.entry_intent(steps=2)
    rig.manager.submit(intent, NOW)
    drive(rig, intent, steps=2)
    events = rig.store.query("SELECT * FROM audit_log WHERE event = 'order_repriced' ORDER BY id")
    assert len(events) == 2


def test_entry_is_cancelled_after_max_reprice_steps(rig):
    intent = rig.entry_intent(steps=2)
    rig.manager.submit(intent, NOW)
    drive(rig, intent, steps=2)
    assert rig.order_of(intent).state == "working"
    result = rig.step(intent, 3 * INTERVAL)  # ladder exhausted -> cancel request
    assert result is None
    assert rig.order_of(intent).state == "cancelled"
    assert rig.refresh(intent)["reason"] == "reprice steps exhausted"
    result = rig.step(intent, 4 * INTERVAL)  # next poll sees the cancel
    assert result == "done"
    final = rig.refresh(intent)
    assert final["status"] == "cancelled"
    assert final["filled_qty"] == 0
    assert final["reason"] == "reprice steps exhausted"
    assert len(rig.order_of(intent).history) == 3  # never repriced after exhaustion


def test_zero_reprice_steps_cancels_at_the_first_interval(rig):
    intent = rig.entry_intent(steps=0)
    rig.manager.submit(intent, NOW)
    rig.step(intent, INTERVAL)
    assert rig.order_of(intent).state == "cancelled"
    assert len(rig.order_of(intent).history) == 1


def test_entry_fills_when_the_limit_crosses():
    rig = Rig(fill_mode="mid")
    intent = rig.entry_intent(steps=3, limit=rig.selection.mid + 0.2)
    rig.manager.submit(intent, NOW)
    assert rig.step(intent, 1) == "filled"
    row = rig.refresh(intent)
    assert (row["status"], row["filled_qty"]) == ("filled", 1)
    assert row["avg_fill_price"] == pytest.approx(row["limit_price"])


# ---------------------------------------------------------------------------------- exits
def test_exit_keeps_working_at_natural_and_notifies_once(rig):
    intent = rig.exit_intent(steps=3)
    rig.manager.submit(intent, NOW)
    legs = rig.selection.legs
    _, natural = close_prices(legs, rig.md.quote_legs(legs))
    for i in range(1, 15):
        assert rig.step(intent, i * INTERVAL) is None
    order = rig.order_of(intent)
    assert order.state == "working"  # never cancelled
    assert rig.refresh(intent)["status"] == "submitted"
    assert order.history == sorted(order.history, reverse=True)  # walks down
    # The last step is marketable: at or below the natural price (the bid), never a tick above
    assert order.limit_price <= natural + 1e-9
    assert order.limit_price == pytest.approx(round_buy(natural, TICK))
    warnings = [n for n in rig.notifier.sent if n[0] == "Exit not yet filled"]
    assert len(warnings) == 1
    assert warnings[0][2] == "warning"
    assert intent["id"] in warnings[0][1]


def test_exit_final_step_is_marketable_when_the_spread_is_narrower_than_a_tick(rig):
    """Regression: natural 0.33 with a 0.05 tick must not stay parked at 0.35."""
    intent = rig.exit_intent(steps=2)
    legs = rig.selection.legs
    narrow = [
        q.__class__(q.symbol, q.expiry, q.strike, q.right, bid, ask, q.delta, 500, q.con_id)
        for q, (bid, ask) in zip(rig.md.quote_legs(legs), [(0.80, 0.90), (0.40, 0.47)], strict=True)
    ]
    rig.md.quote_legs = lambda _legs: narrow  # type: ignore[method-assign]
    mid, natural = close_prices(legs, narrow)
    assert (mid, natural) == (pytest.approx(0.415), pytest.approx(0.33))
    rig.manager.submit(intent, NOW)
    for i in range(1, 6):
        rig.step(intent, i * INTERVAL)
    assert rig.order_of(intent).limit_price <= natural + 1e-9


def test_exit_does_not_reprice_once_at_natural(rig):
    intent = rig.exit_intent(steps=2)
    rig.manager.submit(intent, NOW)
    for i in range(1, 10):
        rig.step(intent, i * INTERVAL)
    history = rig.order_of(intent).history
    # modify() is only called when the price actually changes
    assert all(a != b for a, b in zip(history, history[1:], strict=False))
    assert len(history) <= 3  # initial price plus at most two ladder steps


def test_exit_fills_and_reports_filled():
    rig = Rig(fill_mode="mid")
    intent = rig.exit_intent(steps=3)
    rig.manager.submit(intent, NOW)
    results = [rig.step(intent, i * INTERVAL) for i in range(1, 8)]
    assert "filled" in results
    assert rig.refresh(intent)["status"] == "filled"


# ---------------------------------------------------------------------------- partial fills
def test_partial_fill_then_cancel_is_partially_filled():
    rig = Rig(fill_mode="partial")
    intent = rig.entry_intent(quantity=4, limit=rig.selection.natural + 0.1)
    rig.manager.submit(intent, NOW)
    assert rig.step(intent, INTERVAL) is None  # first poll fills half, still working
    row = rig.refresh(intent)
    assert (row["status"], row["filled_qty"]) == ("submitted", 2)
    rig.manager.cancel(row, NOW + timedelta(seconds=30), "operator")
    result = rig.step(intent, 40)
    assert result == "filled"  # something filled: caller must open a position
    final = rig.refresh(intent)
    assert final["status"] == "partially_filled"
    assert final["filled_qty"] == 2
    assert final["avg_fill_price"] is not None
    events = rig.store.query("SELECT event FROM audit_log WHERE event LIKE 'order_%'")
    assert "order_partially_filled" in [e["event"] for e in events]


def test_cancel_without_fills_ends_cancelled(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    rig.manager.cancel(rig.refresh(intent), NOW + timedelta(seconds=1), "kill switch")
    assert rig.step(intent, 2) == "done"
    final = rig.refresh(intent)
    assert (final["status"], final["reason"]) == ("cancelled", "kill switch")


def test_cancel_survives_a_broker_error(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)

    def broken_cancel(order_id):
        raise ConnectionError("gateway down")

    rig.broker.cancel = broken_cancel  # type: ignore[method-assign]
    rig.manager.cancel(rig.refresh(intent), NOW, "x")
    errors = rig.store.query("SELECT * FROM audit_log WHERE event = 'order_error'")
    assert len(errors) == 1
    assert "cancel failed" in errors[0]["data"]


# ------------------------------------------------------------------------------- errors
def test_broker_submit_exception_marks_intent_error_and_notifies():
    md = FakeMarketData(spots={"NVDA": 180.0}, today=lambda: TODAY)
    rig = Rig(broker=FailingBroker(md))
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    row = rig.refresh(intent)
    assert row["status"] == "error"
    assert "gateway disconnected" in row["reason"]
    assert row["broker_order_id"] is None
    (note,) = rig.notifier.sent
    assert note[0] == "Order submission failed"
    assert note[2] == "error"
    assert intent["id"] in note[1]
    assert len(rig.store.query("SELECT * FROM audit_log WHERE event = 'order_error'")) == 1


def test_rejected_order_without_fill_is_rejected_status(rig):
    intent = rig.entry_intent()
    rig.manager.submit(intent, NOW)
    rig.order_of(intent).state = "rejected"
    assert rig.step(intent, 1) == "done"
    assert rig.refresh(intent)["status"] == "rejected"


def test_legs_net_price_matches_selection(rig):
    legs = rig.selection.legs
    mid, natural = net_prices(legs, rig.md.quote_legs(legs))
    assert mid == pytest.approx(rig.selection.mid)
    assert natural == pytest.approx(rig.selection.natural)
