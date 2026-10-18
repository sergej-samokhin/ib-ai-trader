"""Restart/reconnect recovery, manual corrections and connection alerting (runbook sections 8-9)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from helpers_trading import make_harness, trade_signal
from ibtrader import cli
from ibtrader.app import Trader, run_iteration
from ibtrader.health import ConnectionWatch
from ibtrader.orders import BrokerOrderStatus
from ibtrader.sim import SimBroker

SIGNAL = "nvda-momo-001"


def working_entry(tmp_path):
    """Harness with one entry order working at the (never filling) broker."""
    h = make_harness(tmp_path, fill_mode="never")
    h.submit(trade_signal())
    h.run(2)
    assert h.intent(kind="entry")["status"] == "submitted"
    return h


def restart(h, broker=None) -> Trader:
    trader = Trader(
        h.settings,
        h.trader.registry,
        h.store,
        h.md,
        broker or h.broker,
        h.notifier,
        h.shadow_broker,
    )
    h.trader = trader
    return trader


def fill_while_away(h, qty: int | None = None, state: str = "filled") -> None:
    (order,) = h.broker.orders.values()
    order.filled = order.quantity if qty is None else qty
    order.avg_price = order.limit_price
    order.state = state


# ====================================================================== recovery from the broker
def test_entry_filled_while_away_is_booked_as_position(tmp_path):
    h = working_entry(tmp_path)
    fill_while_away(h)
    restart(h).recover(h.now)

    entry = h.intent(kind="entry")
    assert entry["status"] == "filled"
    (pos,) = h.positions()
    assert (pos["quantity"], pos["entry_price"]) == (entry["quantity"], entry["avg_fill_price"])
    assert pos["signal_id"] == SIGNAL
    assert h.audit_events("order_recovered")
    assert h.notifications("Order state unknown") == []


def test_partial_fill_then_cancel_while_away_books_the_filled_part(tmp_path):
    h = make_harness(
        tmp_path,
        fill_mode="never",
        max_position_usd=2000,
        max_underlying_exposure_usd=2000,
        manual_approval_above_usd=5000,
    )
    h.submit(trade_signal(max_premium_usd=1500, max_contracts=4))
    h.run(2)
    entry = h.intent(kind="entry")
    assert entry["quantity"] > 1
    fill_while_away(h, qty=1, state="cancelled")
    restart(h).recover(h.now)

    assert h.intent(kind="entry")["status"] == "partially_filled"
    assert h.positions()[0]["quantity"] == 1


def test_cancelled_without_fill_while_away_is_cancelled(tmp_path):
    h = working_entry(tmp_path)
    fill_while_away(h, qty=0, state="cancelled")
    restart(h).recover(h.now)
    assert h.intent(kind="entry")["status"] == "cancelled"
    assert h.positions() == []


def test_exit_filled_while_away_closes_position_with_pnl(tmp_path):
    h = make_harness(tmp_path)
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.broker.fill_mode = "never"
    h.md.set_spot("NVDA", 195.0)  # take profit due
    h.run(2)
    exit_intent = h.intent(kind="exit")
    assert exit_intent["status"] == "submitted"
    exit_order = h.broker.orders[exit_intent["broker_order_id"]]
    exit_order.filled, exit_order.avg_price, exit_order.state = (
        exit_order.quantity,
        exit_order.limit_price,
        "filled",
    )

    restart(h).recover(h.now)
    closed = h.store.get_position(pos["id"])
    assert closed["status"] == "closed"
    expected = (exit_order.limit_price - pos["entry_price"]) * 100 * pos["quantity"]
    assert closed["realized_pnl"] == pytest.approx(expected)


class _NoPriceBroker(SimBroker):
    def lookup_final(self, order_ref):
        return BrokerOrderStatus("filled", 1, None, "fill without price")


def test_fill_without_price_stays_unknown(tmp_path):
    h = working_entry(tmp_path)
    restart(h, _NoPriceBroker(h.md, "never")).recover(h.now)
    assert h.intent(kind="entry")["status"] == "unknown"
    assert h.positions() == []
    assert "intent resolve" in h.notifications("Order state unknown")[0][1]


def test_on_reconnect_resets_handles_and_recovers(tmp_path):
    h = working_entry(tmp_path)
    calls = []
    h.broker.reset = lambda: calls.append("reset")  # type: ignore[method-assign]
    fill_while_away(h)
    h.trader.on_reconnect(h.now)
    assert calls == ["reset"]
    assert h.intent(kind="entry")["status"] == "filled"


# ====================================================================== manual resolution
def unknown_entry(tmp_path):
    h = working_entry(tmp_path)
    restart(h, SimBroker(h.md, "never")).recover(h.now)  # broker lost the order
    assert h.intent(kind="entry")["status"] == "unknown"
    return h


def test_resolve_unknown_entry_as_filled_creates_position(tmp_path):
    h = unknown_entry(tmp_path)
    entry = h.intent(kind="entry")
    status = h.trader.resolve_intent(entry["id"], entry["quantity"], 2.55, "ops", h.now)
    assert status == "filled"
    (pos,) = h.positions()
    assert pos["entry_price"] == 2.55
    events = h.audit_events("manual_intent_resolved")
    assert events and '"resolved_by": "ops"' in events[0]["data"]


def test_resolve_unknown_entry_as_not_filled(tmp_path):
    h = unknown_entry(tmp_path)
    entry = h.intent(kind="entry")
    assert h.trader.resolve_intent(entry["id"], 0, None, "ops", h.now) == "cancelled"
    assert h.positions() == []
    # no longer counted as open risk
    assert h.trader.snapshot(h.now, shadow=False).premium_at_risk == 0


def test_unknown_entry_counts_as_risk_until_resolved(tmp_path):
    h = unknown_entry(tmp_path)
    entry = h.intent(kind="entry")
    expected = entry["max_price"] * 100 * entry["quantity"]
    assert h.trader.snapshot(h.now, shadow=False).premium_at_risk == pytest.approx(expected)


@pytest.mark.parametrize(
    ("filled", "price", "message"),
    [(-1, 2.0, "0.."), (99, 2.0, "0.."), (1, None, "price"), (1, 0.0, "price")],
)
def test_resolve_validates_input(tmp_path, filled, price, message):
    h = unknown_entry(tmp_path)
    with pytest.raises(ValueError, match=message):
        h.trader.resolve_intent(h.intent(kind="entry")["id"], filled, price, "ops", h.now)


def test_resolve_refuses_intents_that_are_not_unknown(tmp_path):
    h = working_entry(tmp_path)
    with pytest.raises(ValueError, match="not unknown"):
        h.trader.resolve_intent(h.intent(kind="entry")["id"], 0, None, "ops", h.now)


def test_resolve_unknown_exit_as_filled_closes_position(tmp_path):
    h = make_harness(tmp_path)
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.broker.fill_mode = "never"
    h.md.set_spot("NVDA", 195.0)
    h.run(2)
    restart(h, SimBroker(h.md, "never")).recover(h.now)
    exit_intent = h.intent(kind="exit")
    assert exit_intent["status"] == "unknown"

    h.trader.resolve_intent(exit_intent["id"], pos["quantity"], 5.0, "ops", h.now)
    closed = h.store.get_position(pos["id"])
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] == pytest.approx((5.0 - pos["entry_price"]) * 100)


# ====================================================================== manual position close
def open_position(tmp_path, **kwargs):
    h = make_harness(tmp_path, **kwargs)
    h.submit(trade_signal())
    return h, h.fill_entry()


def test_close_position_manually_books_pnl_and_counts_for_daily_loss(tmp_path):
    h, pos = open_position(tmp_path)
    pnl = h.trader.close_position_manually(pos["id"], 0.5, "ops", h.now)
    assert pnl == pytest.approx((0.5 - pos["entry_price"]) * 100 * pos["quantity"])
    assert h.store.get_position(pos["id"])["status"] == "closed"
    assert h.trader.snapshot(h.now, shadow=False).realized_pnl_today == pytest.approx(pnl)
    assert h.audit_events("manual_position_closed")
    assert h.notifications("Position closed manually")


def test_close_position_manually_supersedes_unknown_exit(tmp_path):
    h, pos = open_position(tmp_path)
    h.broker.fill_mode = "never"
    h.md.set_spot("NVDA", 195.0)
    h.run(2)
    restart(h, SimBroker(h.md, "never")).recover(h.now)
    assert h.intent(kind="exit")["status"] == "unknown"
    h.trader.close_position_manually(pos["id"], 5.0, "ops", h.now)
    assert h.intent(kind="exit")["status"] == "cancelled"


def test_close_position_manually_refuses_while_exit_is_working(tmp_path):
    h, pos = open_position(tmp_path)
    h.broker.fill_mode = "never"
    h.md.set_spot("NVDA", 195.0)
    h.run(2)
    assert h.intent(kind="exit")["status"] == "submitted"
    with pytest.raises(ValueError, match="still working"):
        h.trader.close_position_manually(pos["id"], 5.0, "ops", h.now)
    assert h.store.get_position(pos["id"])["status"] == "open"


def test_close_position_manually_partial_and_validation(tmp_path):
    h, pos = open_position(tmp_path, max_position_usd=2000, max_underlying_exposure_usd=2000)
    assert pos["quantity"] == 1  # default signal sizes to one spread
    with pytest.raises(ValueError, match="quantity"):
        h.trader.close_position_manually(pos["id"], 1.0, "ops", h.now, quantity=2)
    with pytest.raises(ValueError, match="not open"):
        h.trader.close_position_manually("pos_missing", 1.0, "ops", h.now)
    with pytest.raises(ValueError, match=">= 0"):
        h.trader.close_position_manually(pos["id"], -1.0, "ops", h.now)


# ====================================================================== connection alerting
class _Adapter:
    def __init__(self):
        self.polls = 0

    def poll(self, now):
        self.polls += 1


def test_outage_alerts_once_reminds_and_recovers(tmp_path):
    h = working_entry(tmp_path)
    watch = ConnectionWatch(h.notifier, h.trader.audit, reminder_minutes=30)
    adapter = _Adapter()
    ticks = []
    original_tick = h.trader.tick
    h.trader.tick = lambda now: (ticks.append(now), original_tick(now))  # type: ignore[method-assign]

    def down():
        raise ConnectionError("gateway unreachable")

    start = h.now
    for minute in range(0, 45, 5):  # 45 minutes of outage, one loop per 5 minutes
        run_iteration(h.trader, [adapter], watch, start + timedelta(minutes=minute), down)
    assert len(h.notifications("IBKR connection lost")) == 1
    assert len(h.notifications("IBKR still disconnected")) == 1  # at 30 min
    assert ticks == []  # no trading actions while disconnected
    assert adapter.polls == 9  # ingestion kept running
    assert h.audit_events("connection_lost")

    fill_while_away(h)
    run_iteration(h.trader, [adapter], watch, start + timedelta(minutes=50), lambda: True)
    assert h.notifications("IBKR connection restored")
    assert h.audit_events("connection_restored")
    assert h.intent(kind="entry")["status"] == "filled"  # recovery ran on reconnect
    assert len(ticks) == 1
    assert not watch.is_down


def test_healthy_iterations_do_not_alert(tmp_path):
    h = make_harness(tmp_path)
    watch = ConnectionWatch(h.notifier, h.trader.audit)
    for i in range(3):
        run_iteration(h.trader, [], watch, h.now + timedelta(seconds=5 * i), lambda: False)
    assert h.notifications("IBKR") == []


# ====================================================================== CLI
def test_cli_position_close_and_intent_resolve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("PRODUCERS_FILE", str(tmp_path / "none.yaml"))
    assert (
        cli.main(
            ["--env-file", str(tmp_path / "no.env"), "position", "close", "pos_x", "--price", "1.0"]
        )
        == 1
    )
    assert "not open" in capsys.readouterr().out
    assert (
        cli.main(
            ["--env-file", str(tmp_path / "no.env"), "intent", "resolve", "int_x", "--filled", "0"]
        )
        == 1
    )
    assert "unknown intent" in capsys.readouterr().out
