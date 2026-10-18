"""Trader end to end with SimBroker: lifecycle per signal kind, exits, approvals, kill switch,
shadow execution, daily loss limit and failure drills (spec sections 5, 6 and 9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from helpers_trading import (
    NOW,
    FailingBroker,
    Harness,
    default_producers,
    exit_signal,
    make_harness,
    rule_signal,
    trade_signal,
    trending_bars,
)
from ibtrader.app import Trader
from ibtrader.producers import Producer
from ibtrader.sim import SimBroker

SIGNAL = "nvda-momo-001"


def utc(year, month, day, hour, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.fixture
def h(tmp_path) -> Harness:
    return make_harness(tmp_path)


def big_harness(tmp_path, **kwargs) -> Harness:
    """Limits wide enough for multi-contract positions (4 spreads at about 274 USD each)."""
    risk = {
        "max_position_usd": 2000.0,
        "max_portfolio_risk_usd": 5000.0,
        "max_underlying_exposure_usd": 5000.0,
        "manual_approval_above_usd": 5000.0,
        **kwargs.pop("risk", {}),
    }
    producers = [Producer(id=p.id, status=p.status, budget_usd=5000) for p in default_producers()]
    return make_harness(tmp_path, producers=producers, **risk, **kwargs)


def big_trade(signal_id: str = SIGNAL, **kwargs) -> str:
    return trade_signal(signal_id, max_premium_usd=1200, max_contracts=4, **kwargs)


def audit_data(h: Harness, event: str) -> list[dict]:
    return [json.loads(row["data"]) for row in h.audit_events(event)]


def close_position(h: Harness, spot: float | None = None) -> None:
    if spot is not None:
        h.md.set_spot("NVDA", spot)
    h.run_until(lambda: not h.store.open_positions())


# ==================================================================== entry -> fill -> position
def test_trade_signal_becomes_position_linked_to_signal(h):
    ack = h.submit(trade_signal(), "momentum-agent")
    assert ack.status == "accepted"
    assert h.signal_status("momentum-agent", SIGNAL) == "pending"

    pos = h.fill_entry()

    assert (pos["producer"], pos["signal_id"], pos["version"]) == ("momentum-agent", SIGNAL, 1)
    assert (pos["underlying"], pos["strategy"], pos["shadow"]) == ("NVDA", "bull_call_spread", 0)
    assert pos["quantity"] == 1
    assert pos["status"] == "open"
    assert json.loads(pos["exit_rules"]) == {
        "take_profit_pct": 50.0,
        "stop_loss_pct": 50.0,
        "close_before_expiry_days": 5,
    }
    assert pos["max_reprice_steps"] == 3
    intent = h.intent(kind="entry")
    assert intent["status"] == "filled"
    assert intent["position_id"] == pos["id"]
    assert intent["id"] == pos["intent_id"]
    assert pos["entry_price"] <= intent["max_price"] + 1e-9
    assert pos["entry_price"] == pytest.approx(intent["avg_fill_price"])
    assert h.signal_status("momentum-agent", SIGNAL) == "consumed"
    assert h.audit_events("position_opened")
    assert h.notifications("Filled")


def test_spread_legs_are_stored_as_one_combo_order(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    assert len(h.broker.orders) == 1
    (order,) = h.broker.orders.values()
    assert [(leg.right, leg.action) for leg in order.legs] == [("C", "BUY"), ("C", "SELL")]
    assert order.legs[0].strike < order.legs[1].strike
    assert order.action == "BUY"
    stored = json.loads(pos["legs"])
    assert [leg["action"] for leg in stored] == ["BUY", "SELL"]
    assert len(json.loads(h.intent(kind="entry")["legs"])) == 2
    assert order.order_ref == h.intent(kind="entry")["order_ref"]
    assert order.order_ref.startswith(f"momentum-agent|{SIGNAL}|v1|int_")


@pytest.mark.parametrize(
    ("strategy", "legs", "right"),
    [
        ("long_call", 1, "C"),
        ("long_put", 1, "P"),
        ("bull_call_spread", 2, "C"),
        ("bear_put_spread", 2, "P"),
    ],
)
def test_every_strategy_runs_through_the_lifecycle(tmp_path, strategy, legs, right):
    h = make_harness(tmp_path, spots={"AMD": 40.0})
    h.submit(trade_signal("amd-test-001", underlying="AMD", strategy=strategy))
    pos = h.fill_entry("amd-test-001")
    stored = json.loads(pos["legs"])
    assert len(stored) == legs
    assert {leg["right"] for leg in stored} == {right}
    assert len(h.broker.orders) == 1  # spreads are never legged in
    assert len(next(iter(h.broker.orders.values())).legs) == legs


# ================================================================================== exits
def test_take_profit_exit(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.md.set_spot("NVDA", 195.0)
    close_position(h)

    exit_intent = h.intent(kind="exit")
    assert exit_intent["exit_reason"] == "take_profit"
    assert exit_intent["status"] == "filled"
    assert exit_intent["position_id"] == pos["id"]
    assert exit_intent["max_price"] is None
    closed = h.store.get_position(pos["id"])
    assert (closed["status"], closed["quantity"]) == ("closed", 0)
    expected = (exit_intent["avg_fill_price"] - pos["entry_price"]) * 100
    assert closed["realized_pnl"] == pytest.approx(expected)
    assert closed["realized_pnl"] > 0
    assert h.notifications("Exit filled")


def test_stop_loss_exit(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.md.set_spot("NVDA", 165.0)
    close_position(h)

    exit_intent = h.intent(kind="exit")
    assert exit_intent["exit_reason"] == "stop_loss"
    closed = h.store.get_position(pos["id"])
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] < 0
    assert exit_intent["avg_fill_price"] <= pos["entry_price"] * 0.5 + 0.15


def test_time_exit_before_expiry(h):
    h.submit(trade_signal(close_before_expiry_days=5))
    pos = h.fill_entry()
    expiry = json.loads(pos["legs"])[0]["expiry"]
    assert expiry == "2026-11-20"
    # Monday 2026-11-16, 10:00 EST: DTE 4 <= 5. Spot unchanged, so no TP / SL involved.
    h.at(utc(2026, 11, 16, 15, 0))
    close_position(h)
    assert h.intent(kind="exit")["exit_reason"] == "time_exit"
    assert h.store.get_position(pos["id"])["status"] == "closed"


def test_no_time_exit_while_outside_the_dte_threshold(h):
    h.submit(trade_signal(close_before_expiry_days=5, take_profit_pct=1000, stop_loss_pct=100))
    h.fill_entry()
    h.at(utc(2026, 11, 13, 15, 0))  # Friday, DTE 7
    h.run(8)
    assert h.intents(kind="exit") == []


def test_positions_are_not_monitored_while_the_market_is_closed(h):
    h.submit(trade_signal())
    h.fill_entry()
    h.md.set_spot("NVDA", 195.0)
    h.at(utc(2026, 10, 19, 21, 0))  # 17:00 New York
    h.run(4)
    assert h.intents(kind="exit") == []
    h.at(utc(2026, 10, 20, 14, 0))
    h.run_until(lambda: h.intents(kind="exit"))


def test_exit_signal_closes_the_position(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    ack = h.submit(exit_signal(SIGNAL))
    assert ack.status == "accepted"
    close_position(h)

    exit_intent = h.intent(kind="exit")
    assert exit_intent["exit_reason"] == f"exit_signal:{SIGNAL}-exit"
    assert exit_intent["quantity"] == pos["quantity"]
    assert h.store.get_position(pos["id"])["status"] == "closed"
    assert h.signal_status("momentum-agent", f"{SIGNAL}-exit") == "consumed"


def test_exit_signal_reduce_halves_the_position(tmp_path):
    h = big_harness(tmp_path)
    h.submit(big_trade())
    pos = h.fill_entry()
    assert pos["quantity"] == 4

    h.submit(exit_signal(SIGNAL, action="reduce", reduce_pct=50))
    h.run_until(lambda: h.intents(kind="exit", status="filled"))
    remaining = h.store.get_position(pos["id"])
    assert (remaining["status"], remaining["quantity"]) == ("open", 2)
    assert h.intent(kind="exit")["quantity"] == 2
    assert h.intent(kind="exit")["exit_reason"].startswith("exit_signal:")
    assert h.signal_status("momentum-agent", f"{SIGNAL}-exit") == "consumed"


def test_exit_signal_cancels_an_unfilled_entry(tmp_path):
    h = make_harness(tmp_path, fill_mode="never")
    h.submit(trade_signal())
    h.run(2)
    entry = h.intent(kind="entry")
    assert entry["status"] == "submitted"
    (order_id,) = h.broker.orders

    h.submit(exit_signal(SIGNAL))
    h.run(2)

    entry = h.intent(kind="entry")
    assert entry["status"] == "cancelled"
    assert entry["reason"] == f"exit signal {SIGNAL}-exit"
    assert h.broker.orders[order_id].state == "cancelled"
    assert h.positions() == []
    assert h.signal_status("momentum-agent", f"{SIGNAL}-exit") == "consumed"


def test_exit_signal_without_position_or_entry_is_no_position(h):
    h.submit(trade_signal())
    h.fill_entry()
    close_position(h, spot=195.0)  # take profit closes everything
    h.submit(exit_signal(SIGNAL))
    h.run(2)
    assert h.signal_status("momentum-agent", f"{SIGNAL}-exit") == "no_position"
    assert len(h.intents(kind="exit")) == 1


def test_exit_signal_for_another_producer_cannot_close_a_position(h):
    h.submit(trade_signal())
    h.fill_entry()
    ack = h.submit(exit_signal(SIGNAL, producer_id="macro-agent"), "macro-agent")
    assert ack.status == "rejected"  # unknown target for this producer
    h.run(2)
    assert h.store.open_positions()


def test_only_one_exit_intent_per_position(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.broker.fill_mode = "never"
    first = h.trader.create_exit(pos, 1, "manual", h.now)
    assert first is not None
    assert h.trader.create_exit(pos, 1, "manual", h.now) is None
    assert len(h.intents(kind="exit")) == 1


# ============================================================================ entry window / TTL
def test_trade_outside_the_entry_window_stays_pending_until_it_opens(h):
    h.submit(trade_signal(), now=utc(2026, 10, 19, 13, 35))
    h.at(utc(2026, 10, 19, 13, 35)).run(1)
    assert h.signal_status("momentum-agent", SIGNAL) == "pending"
    assert h.intents() == []

    h.at(utc(2026, 10, 19, 13, 44, 59)).run(1)  # 9:44:59 New York
    assert h.signal_status("momentum-agent", SIGNAL) == "pending"
    assert h.intents() == []

    h.at(utc(2026, 10, 19, 13, 45)).run(1)  # 9:45 New York
    assert h.signal_status("momentum-agent", SIGNAL) == "consumed"
    h.fill_entry()


def test_trade_after_the_close_waits_for_the_next_session(h):
    h.submit(trade_signal(valid_until="2026-10-20T19:55:00Z"), now=utc(2026, 10, 19, 21, 0))
    h.at(utc(2026, 10, 19, 21, 0)).run(2)
    assert h.signal_status("momentum-agent", SIGNAL) == "pending"
    h.at(utc(2026, 10, 20, 14, 30))
    h.fill_entry()


def test_pending_trade_expires_when_valid_until_passes(h):
    h.submit(trade_signal(valid_until="2026-10-19T13:40:00Z"), now=utc(2026, 10, 19, 13, 35))
    h.at(utc(2026, 10, 19, 13, 41)).run(1)  # window still closed
    assert h.signal_status("momentum-agent", SIGNAL) == "expired"
    assert h.intents() == []
    assert h.audit_events("signal_expired")
    h.at(utc(2026, 10, 19, 14, 30)).run(2)
    assert h.intents() == []  # an expired signal never trades later


def test_active_rule_expires_when_valid_until_passes(h):
    h.submit(rule_signal(valid_until="2026-10-19T16:00:00Z"))
    assert h.signal_status("momentum-agent", "aapl-sma-cross") == "active"
    h.at(utc(2026, 10, 19, 16, 0, 1)).run(1)
    assert h.signal_status("momentum-agent", "aapl-sma-cross") == "expired"


def test_expired_signal_reason_is_recorded_in_history(h):
    h.submit(trade_signal(valid_until="2026-10-19T13:40:00Z"), now=utc(2026, 10, 19, 13, 35))
    h.at(utc(2026, 10, 19, 13, 41)).run(1)
    history = h.store.signal_history("momentum-agent", SIGNAL)
    assert [row["status"] for row in history] == ["pending", "expired"]
    assert history[-1]["reason"] == "valid_until"


# ====================================================================================== rules
def test_condition_rule_fires_once_at_check_at(h):
    h.md.bars["AAPL"] = trending_bars(up=True)
    h.submit(rule_signal(check_at="15:30 America/New_York"))  # 19:30 UTC in October

    h.at(utc(2026, 10, 19, 19, 29, 30)).run(1)
    assert h.audit_events("rule_evaluated") == []  # before check_at nothing is evaluated

    h.at(utc(2026, 10, 19, 19, 30)).run(1)
    (evaluation,) = audit_data(h, "rule_evaluated")
    assert evaluation["fired"] is True
    assert evaluation["values"]["close"] > evaluation["values"]["sma_20"]
    assert evaluation["values"]["rsi_14"] < 70
    entry = h.intent(kind="entry")
    assert (entry["producer"], entry["signal_id"]) == ("momentum-agent", "aapl-sma-cross")
    assert entry["status"] in ("approved", "submitted")

    h.run(12)  # the rest of the entry window
    assert len(audit_data(h, "rule_evaluated")) == 1
    assert len(h.intents(kind="entry")) == 1
    assert h.signal_status("momentum-agent", "aapl-sma-cross") == "active"  # rule stays active


def test_condition_rule_that_does_not_fire_is_evaluated_once_per_day(h):
    h.md.bars["AAPL"] = trending_bars(up=False)
    h.submit(rule_signal(check_at="15:30 America/New_York"))
    h.at(utc(2026, 10, 19, 19, 30)).run(10)
    (evaluation,) = audit_data(h, "rule_evaluated")
    assert evaluation["fired"] is False
    assert h.intents() == []
    # next trading day it is evaluated again
    h.at(utc(2026, 10, 20, 19, 30)).run(2)
    assert len(audit_data(h, "rule_evaluated")) == 2


def test_rule_respects_max_entries_per_day(h):
    h.md.bars["AAPL"] = trending_bars(up=True)
    h.submit(rule_signal(check_at=None, max_entries_per_day=2))  # interval based, in window
    h.run(30, seconds=60)  # 14:30 - 15:00, many 5-minute intervals
    assert len(h.intents(kind="entry", signal_id="aapl-sma-cross")) == 2
    assert len(audit_data(h, "rule_evaluated")) == 2  # capped rules are no longer evaluated
    # a new New York day resets the counter
    h.at(utc(2026, 10, 20, 14, 30)).run(1)
    assert len(h.intents(kind="entry", signal_id="aapl-sma-cross")) == 3


def test_rule_without_check_at_waits_for_the_entry_window(h):
    h.md.bars["AAPL"] = trending_bars(up=True)
    h.submit(rule_signal(check_at=None), now=utc(2026, 10, 19, 13, 35))
    h.at(utc(2026, 10, 19, 13, 40)).run(1)
    assert h.audit_events("rule_evaluated") == []
    h.at(utc(2026, 10, 19, 13, 45)).run(1)
    assert len(audit_data(h, "rule_evaluated")) == 1


def test_rule_is_not_evaluated_on_a_holiday(h):
    h.md.bars["AAPL"] = trending_bars(up=True)
    h.submit(rule_signal(check_at="10:30 America/New_York"))
    h.at(utc(2026, 11, 26, 16, 0)).run(2)  # Thanksgiving
    assert h.audit_events("rule_evaluated") == []


def test_schedule_rule_fires(h):
    h.submit(
        rule_signal(
            "qqq-weekly-put-spread",
            producer_id="macro-agent",
            underlying="QQQ",
            strategy="bear_put_spread",
            condition=None,
            check_at="10:30 America/New_York",
            long_delta=0.35,
            short_delta=0.15,
            version=2,
        ),
        "macro-agent",
    )
    h.at(NOW - timedelta(seconds=30)).run(1)
    assert h.intents() == []
    h.at(NOW).run(1)
    entry = h.intent(kind="entry")
    assert (entry["producer"], entry["signal_id"], entry["version"]) == (
        "macro-agent",
        "qqq-weekly-put-spread",
        2,
    )
    assert entry["strategy"] == "bear_put_spread"
    assert entry["status"] in ("approved", "submitted")
    pos = h.fill_entry("qqq-weekly-put-spread", "macro-agent")
    assert pos["version"] == 2
    h.run(10)
    assert len(h.intents(kind="entry")) == 1  # max_entries_per_day = 1


# ================================================================================== approvals
def test_manual_producer_waits_for_approval_then_submits(h):
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(3)
    intent = h.intent(kind="entry")
    assert intent["status"] == "awaiting_approval"
    assert intent["approval_deadline"] == "2026-10-19T15:00:00+00:00"
    assert "manual" in intent["reason"]
    assert h.broker.orders == {}
    (note,) = h.notifications("Approval needed")
    assert note[2] == "warning"
    assert intent["id"] in note[1]
    assert "untrusted" in note[1]

    assert h.trader.approve(intent["id"], "sergej", h.now) is True
    assert h.intent(kind="entry")["status"] == "approved"
    h.run(1)
    assert h.intent(kind="entry")["status"] in ("submitted", "filled")
    assert h.intent(kind="entry")["approved_by"] == "sergej"
    assert len(h.broker.orders) == 1
    h.fill_entry(producer_id="manual-agent")


def test_approval_timeout_expires_the_intent(h):
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    intent_id = h.intent(kind="entry")["id"]
    h.at(NOW + timedelta(minutes=29, seconds=59)).run(1)
    assert h.intent(kind="entry")["status"] == "awaiting_approval"
    h.at(NOW + timedelta(minutes=30)).run(1)
    assert h.intent(kind="entry")["status"] == "expired"
    assert h.intent(kind="entry")["reason"] == "approval timeout"
    assert h.notifications("Approval expired")
    assert h.audit_events("approval_expired")
    assert h.trader.approve(intent_id, "sergej", h.now) is False  # too late
    h.run(2)
    assert h.broker.orders == {}


def test_reject_closes_the_intent_and_informs_the_producer(h):
    events: list[tuple[str, dict]] = []
    h.trader.feedback_sinks.append(lambda producer, payload: events.append((producer, payload)))
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    intent_id = h.intent(kind="entry")["id"]

    assert h.trader.reject(intent_id, "sergej", h.now) is True
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "manual")
    assert "sergej" in row["reason"]
    assert any(p["event"] == "rejected" and p["rule"] == "manual" for _, p in events)
    assert h.trader.reject(intent_id, "sergej", h.now) is False
    assert h.trader.approve(intent_id, "sergej", h.now) is False
    h.run(3)
    assert h.broker.orders == {}


def test_approve_and_reject_ignore_unknown_intents(h):
    assert h.trader.approve("int_nope", "me", h.now) is False
    assert h.trader.reject("int_nope", "me", h.now) is False


def test_approval_after_the_window_closed_is_rejected_on_refresh(h):
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    intent_id = h.intent(kind="entry")["id"]
    assert h.trader.approve(intent_id, "sergej", h.now)
    h.at(utc(2026, 10, 19, 19, 50)).run(1)  # inside the last 15 minutes
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "market_hours")
    assert h.broker.orders == {}


def test_approval_with_a_moved_market_is_rejected_on_refresh(h):
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    intent = h.intent(kind="entry")
    h.trader.approve(intent["id"], "sergej", h.now)
    h.md.set_spot("NVDA", 205.0)  # the spread now trades well above the approved max price
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "price_moved")
    assert h.broker.orders == {}


def test_low_confidence_needs_approval(tmp_path):
    h = make_harness(tmp_path, min_confidence_auto=0.7)
    h.submit(trade_signal(confidence=0.62))
    h.run(1)
    intent = h.intent(kind="entry")
    assert intent["status"] == "awaiting_approval"
    assert "confidence" in intent["reason"]


# ================================================================================ kill switch
def test_kill_switch_cancels_entries_keeps_exits_and_blocks_new_entries(tmp_path):
    h = make_harness(tmp_path)
    h.submit(trade_signal(SIGNAL))
    pos_a = h.fill_entry(SIGNAL)

    h.broker.fill_mode = "never"  # from here on nothing fills
    h.submit(trade_signal("nvda-momo-002"))
    h.run(2)
    entry_b = h.intent(signal_id="nvda-momo-002")
    assert entry_b["status"] == "submitted"
    h.trader.create_exit(pos_a, pos_a["quantity"], "manual", h.now)
    h.run(2)
    exit_a = h.intent(kind="exit")
    assert exit_a["status"] == "submitted"

    h.trader.kill_switch.activate("drill", h.now)
    h.run(1)

    entry_b = h.intent(signal_id="nvda-momo-002")
    assert (entry_b["status"], entry_b["reason"]) == ("cancelled", "kill switch")
    assert h.broker.orders[entry_b["broker_order_id"]].state == "cancelled"
    exit_a = h.intent(kind="exit")
    assert exit_a["status"] == "submitted"
    assert h.broker.orders[exit_a["broker_order_id"]].state == "working"
    (alert,) = h.notifications("KILL SWITCH ACTIVE")
    assert alert[2] == "critical"
    assert audit_data(h, "kill_switch_activated")[0]["entries_cancelled"] == 1

    # new entries are blocked while it is active
    h.submit(trade_signal("nvda-momo-003"))
    h.run(2)
    blocked = h.intent(signal_id="nvda-momo-003")
    assert (blocked["status"], blocked["rule"]) == ("rejected", "kill_switch")

    # exits still go through
    h.broker.fill_mode = "mid"
    h.run_until(lambda: h.intent(kind="exit")["status"] == "filled")
    assert h.store.get_position(pos_a["id"])["status"] == "closed"
    assert h.trader.kill_switch.is_active()

    # releasing it allows entries again, and says so
    h.trader.kill_switch.deactivate()
    h.run(1)
    assert h.notifications("Kill switch released")
    h.submit(trade_signal("nvda-momo-004"))
    h.run(2)
    assert h.intent(signal_id="nvda-momo-004")["status"] in ("approved", "submitted")


def test_kill_switch_activation_is_handled_once(h):
    h.trader.kill_switch.activate("drill", h.now)
    h.run(5)
    assert len(h.notifications("KILL SWITCH ACTIVE")) == 1
    assert len(h.audit_events("kill_switch_activated")) == 1


def test_kill_switch_cancels_intents_awaiting_approval(h):
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    assert h.intent(kind="entry")["status"] == "awaiting_approval"
    h.trader.kill_switch.activate("drill", h.now)
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("cancelled", "kill_switch")
    assert h.trader.approve(row["id"], "sergej", h.now) is False


def test_kill_switch_rejects_pending_trades_even_outside_the_window(h):
    h.submit(trade_signal(valid_until="2026-10-20T19:55:00Z"), now=utc(2026, 10, 19, 21, 0))
    h.at(utc(2026, 10, 19, 21, 0))
    h.trader.kill_switch.activate("drill", h.now)
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "kill_switch")


def test_kill_switch_file_lives_in_the_data_dir(h):
    assert h.trader.kill_switch.flag_file == h.settings.data_dir / "KILL"
    (h.settings.data_dir / "KILL").write_text("manual\n")
    h.run(1)
    assert h.notifications("KILL SWITCH ACTIVE")


def test_partial_fill_cancelled_by_kill_switch_keeps_the_filled_part(tmp_path):
    h = big_harness(tmp_path, fill_mode="partial")
    h.submit(big_trade())
    h.run_until(lambda: h.intents(kind="entry", filled_qty=2))
    assert h.intent(kind="entry")["status"] == "submitted"
    assert h.positions() == []

    h.trader.kill_switch.activate("drill", h.now)
    h.run(1)

    entry = h.intent(kind="entry")
    assert (entry["status"], entry["filled_qty"]) == ("partially_filled", 2)
    (pos,) = h.positions()
    assert pos["quantity"] == 2
    assert entry["position_id"] == pos["id"]


# ===================================================================================== shadow
def live_harness(tmp_path, **kwargs) -> Harness:
    h = make_harness(tmp_path, mode="live", **kwargs)
    assert h.settings.is_live
    return h


def test_paper_only_producer_in_live_mode_trades_on_the_shadow_broker_only(tmp_path):
    h = live_harness(tmp_path)
    assert h.broker is not h.shadow_broker
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    pos = h.fill_entry(producer_id="paper-agent")

    assert pos["shadow"] == 1
    assert h.intent(kind="entry")["shadow"] == 1
    assert h.broker.orders == {}
    assert len(h.shadow_broker.orders) == 1
    assert h.notifications("[SHADOW]")


def test_shadow_exit_also_stays_on_the_shadow_broker(tmp_path):
    h = live_harness(tmp_path)
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    h.fill_entry(producer_id="paper-agent")
    h.md.set_spot("NVDA", 195.0)
    close_position(h)
    assert h.intent(kind="exit")["shadow"] == 1
    assert h.broker.orders == {}
    assert len(h.shadow_broker.orders) == 2  # entry and exit


def test_shadow_pnl_is_kept_apart_from_real_pnl(tmp_path):
    h = live_harness(tmp_path)
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    h.fill_entry(producer_id="paper-agent")
    close_position(h, spot=195.0)
    midnight = utc(2026, 10, 19, 4, 0)
    assert h.store.realized_pnl_since(midnight, shadow=False) == 0.0
    assert h.store.realized_pnl_since(midnight, shadow=True) > 0
    (card,) = [c for c in h.store.scorecard() if c["producer"] == "paper-agent"]
    assert card["pnl"] == 0
    assert card["shadow_pnl"] > 0
    assert h.trader.reconcile() == {}  # shadow positions never show up against the real account


def test_auto_producer_in_live_mode_uses_the_real_broker(tmp_path):
    h = live_harness(tmp_path)
    h.submit(trade_signal())
    pos = h.fill_entry()
    assert pos["shadow"] == 0
    assert len(h.broker.orders) == 1
    assert h.shadow_broker.orders == {}


def test_paper_only_producer_in_paper_mode_uses_the_main_broker(tmp_path):
    h = make_harness(tmp_path, mode="paper")
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    pos = h.fill_entry(producer_id="paper-agent")
    assert pos["shadow"] == 0
    assert len(h.broker.orders) == 1
    assert h.shadow_broker.orders == {}


def test_shadow_intents_never_need_approval_but_real_ones_do(tmp_path):
    h = live_harness(tmp_path, min_confidence_auto=0.9)
    h.submit(trade_signal("shadow-sig-001", producer_id="paper-agent"), "paper-agent")
    h.submit(trade_signal("real-sig-001", underlying="AMD"), "momentum-agent")
    h.run(1)
    assert h.intent(signal_id="shadow-sig-001")["status"] in ("approved", "submitted")
    assert h.intent(signal_id="real-sig-001")["status"] == "awaiting_approval"


def test_shadow_positions_do_not_count_against_real_limits(tmp_path):
    h = live_harness(tmp_path, max_open_positions=1)
    h.submit(trade_signal("shadow-sig-001", producer_id="paper-agent"), "paper-agent")
    h.fill_entry("shadow-sig-001", "paper-agent")
    h.submit(trade_signal("real-sig-001", underlying="AMD"), "momentum-agent")
    h.fill_entry("real-sig-001")
    assert sorted(p["shadow"] for p in h.positions()) == [0, 1]


def test_kill_switch_still_blocks_shadow_entries(tmp_path):
    h = live_harness(tmp_path)
    h.trader.kill_switch.activate("drill", h.now)
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"], row["shadow"]) == ("rejected", "kill_switch", 1)


# =========================================================================== risk via lifecycle
def test_daily_loss_limit_stops_new_entries_after_a_losing_close(tmp_path):
    h = make_harness(tmp_path, max_daily_loss_usd=100.0)
    h.submit(trade_signal(SIGNAL))
    pos = h.fill_entry()
    close_position(h, spot=150.0)
    assert h.store.get_position(pos["id"])["realized_pnl"] <= -100.0

    h.submit(trade_signal("nvda-momo-002"))
    h.run(2)
    rejected = h.intent(signal_id="nvda-momo-002")
    assert (rejected["status"], rejected["rule"]) == ("rejected", "daily_loss_limit")
    assert h.broker.orders and len(h.broker.orders) == 2  # entry + exit of the first trade only

    # the counter is per New York day
    h.submit(trade_signal("nvda-momo-003", valid_until="2026-10-20T19:55:00Z"))
    h.at(utc(2026, 10, 20, 14, 30)).run(1)
    assert h.intent(signal_id="nvda-momo-003")["status"] in ("approved", "submitted")


def test_daily_loss_limit_does_not_block_exits(tmp_path):
    h = make_harness(tmp_path, max_daily_loss_usd=100.0)
    h.submit(trade_signal(SIGNAL))
    h.submit(trade_signal("amd-test-001", underlying="AMD"))
    h.fill_entry(SIGNAL)
    h.fill_entry("amd-test-001")
    h.md.set_spot("NVDA", 150.0)  # NVDA loses more than the daily limit ...
    h.run_until(
        lambda: any(
            p["underlying"] == "NVDA"
            for p in h.store.query("SELECT * FROM positions WHERE status = 'closed'")
        )
    )
    h.md.set_spot("AMD", 90.0)  # ... AMD must still be able to exit afterwards
    close_position(h)
    assert h.store.open_positions() == []


def test_max_open_positions_rejection_is_audited_with_rule_and_producer(tmp_path):
    h = make_harness(tmp_path, max_open_positions=1)
    h.submit(trade_signal(SIGNAL))
    h.fill_entry()
    h.submit(
        trade_signal("amd-test-001", underlying="AMD", producer_id="macro-agent"), "macro-agent"
    )
    h.run(2)
    rejected = h.intent(signal_id="amd-test-001")
    assert (rejected["status"], rejected["rule"]) == ("rejected", "max_open_positions")
    (event,) = [e for e in h.audit_events("risk_rejected")]
    assert event["producer"] == "macro-agent"
    assert json.loads(event["data"])["rule"] == "max_open_positions"
    assert h.notifications("Rejected: AMD")


def test_open_entry_orders_count_against_the_position_limit(tmp_path):
    h = make_harness(tmp_path, max_open_positions=1, fill_mode="never")
    h.submit(trade_signal(SIGNAL))
    h.run(2)
    h.submit(trade_signal("amd-test-001", underlying="AMD"))
    h.run(1)
    assert h.intent(signal_id="amd-test-001")["rule"] == "max_open_positions"


def test_underlying_concentration_across_producers(tmp_path):
    h = make_harness(tmp_path, max_underlying_exposure_usd=300.0, fill_mode="never")
    h.submit(trade_signal(SIGNAL))
    h.run(2)
    h.submit(trade_signal("nvda-macro-001", producer_id="macro-agent"), "macro-agent")
    h.run(1)
    other = h.intent(signal_id="nvda-macro-001")
    assert (other["status"], other["rule"]) == ("rejected", "underlying_concentration")


def test_producer_budget_is_enforced(tmp_path):
    producers = [Producer(id="momentum-agent", status="auto", budget_usd=100)]
    h = make_harness(tmp_path, producers=producers)
    h.submit(trade_signal())
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "producer_budget")


def test_runtime_pause_rejects_queued_signals(h):
    h.submit(trade_signal())
    h.store.pause_producer("momentum-agent", "drill", h.now)
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "producer_paused")
    assert h.broker.orders == {}


def test_selection_failure_is_a_rejected_intent(h):
    h.submit(trade_signal(dte_min=200, dte_max=210))
    h.run(1)
    row = h.intent(kind="entry")
    assert (row["status"], row["rule"]) == ("rejected", "contract_selection")
    assert "no expiry" in row["reason"]
    assert h.notifications("Rejected: NVDA")


def test_signal_cannot_exceed_the_global_position_cap(tmp_path):
    h = make_harness(tmp_path, max_position_usd=300.0, manual_approval_above_usd=5000.0)
    h.submit(trade_signal(max_premium_usd=100_000, max_contracts=100))
    h.run(1)
    row = h.intent(kind="entry")
    assert row["quantity"] == 1  # floor(300 / 274)
    assert row["quantity"] * row["max_price"] * 100 <= 300.0
    assert row["max_price"] <= row["natural"] + 1e-9


# ================================================================================ failure drills
def test_recover_reattaches_working_orders_by_order_ref(tmp_path):
    h = make_harness(tmp_path, fill_mode="never")
    h.submit(trade_signal())
    h.run(2)
    entry = h.intent(kind="entry")
    real_order_id = entry["broker_order_id"]
    h.store.update_intent(entry["id"], h.now, broker_order_id="stale-id")  # id lost on restart

    restarted = Trader(
        h.settings, h.trader.registry, h.store, h.md, h.broker, h.notifier, h.shadow_broker
    )
    restarted.recover(h.now)

    row = h.intent(kind="entry")
    assert row["status"] == "submitted"
    assert row["broker_order_id"] == real_order_id
    assert h.audit_events("order_reattached")

    h.trader = restarted
    h.broker.fill_mode = "mid"
    h.fill_entry()
    assert h.intent(kind="entry")["status"] == "filled"


def test_recover_marks_orders_missing_at_the_broker_unknown_and_notifies(tmp_path):
    h = make_harness(tmp_path, fill_mode="never")
    h.submit(trade_signal())
    h.run(2)
    entry = h.intent(kind="entry")
    assert entry["status"] == "submitted"

    amnesiac_broker = SimBroker(h.md, "never")  # restarted gateway without that order
    restarted = Trader(
        h.settings, h.trader.registry, h.store, h.md, amnesiac_broker, h.notifier, h.shadow_broker
    )
    restarted.recover(h.now)

    row = h.intent(kind="entry")
    assert row["status"] == "unknown"
    assert "not found at broker" in row["reason"]
    (note,) = h.notifications("Order state unknown")
    assert note[2] == "error"
    assert entry["order_ref"] in note[1]
    assert h.audit_events("order_unknown")
    h.trader = restarted
    h.run(3)
    assert h.intent(kind="entry")["status"] == "unknown"  # left for a human to reconcile
    assert amnesiac_broker.orders == {}


def test_recover_drops_simulated_shadow_orders(tmp_path):
    h = live_harness(tmp_path, shadow_fill_mode="never")
    h.submit(trade_signal(producer_id="paper-agent"), "paper-agent")
    h.run(2)
    assert h.intent(kind="entry")["status"] == "submitted"
    h.trader.recover(h.now)
    row = h.intent(kind="entry")
    assert (row["status"], row["reason"]) == ("cancelled", "simulated order lost on restart")
    assert h.notifications("Order state unknown") == []


def test_recover_ignores_orders_that_are_not_submitted(tmp_path):
    h = make_harness(tmp_path)
    h.submit(trade_signal(producer_id="manual-agent"), "manual-agent")
    h.run(1)
    h.trader.recover(h.now)
    assert h.intent(kind="entry")["status"] == "awaiting_approval"


def test_market_data_failure_in_one_step_is_caught_and_the_next_tick_works(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.md.set_spot("NVDA", 195.0)  # a take profit is due ...
    original = h.md.quote_legs

    def feed_down(legs):
        raise RuntimeError("market data feed down")

    h.md.quote_legs = feed_down  # type: ignore[method-assign]
    h.run(1)  # ... but the monitor cannot price anything: must not raise

    errors = audit_data(h, "error")
    assert errors
    assert errors[0]["step"] == "_monitor_positions"
    assert "feed down" in errors[0]["error"]
    assert h.audit_events("error")[0]["ts"] is not None
    (alert,) = h.notifications("Trader error")[:1]
    assert alert[2] == "error"
    assert h.intents(kind="exit") == []
    assert h.store.get_position(pos["id"])["status"] == "open"

    h.md.quote_legs = original  # type: ignore[method-assign]
    close_position(h)
    assert h.intent(kind="exit")["exit_reason"] == "take_profit"


def test_a_failing_step_does_not_stop_the_later_steps(h):
    def broken_bars(symbol, count):
        raise RuntimeError("history service down")

    h.md.daily_bars = broken_bars  # type: ignore[method-assign]
    h.submit(rule_signal(check_at=None))
    h.submit(trade_signal())
    h.fill_entry()  # entry, order management and fills still work while _evaluate_rules fails
    steps = {e["step"] for e in audit_data(h, "error")}
    assert steps == {"_evaluate_rules"}


def test_market_data_failure_on_entry_does_not_break_later_signals(h):
    calls = {"n": 0}
    original = h.md.expirations

    def flaky(symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chain request timed out")
        return original(symbol)

    h.md.expirations = flaky  # type: ignore[method-assign]
    h.submit(trade_signal("nvda-momo-001"))
    h.run(1)
    # Not silent: the signal gets a rejected intent with rule 'market_data' + producer feedback.
    rejected = h.intent(signal_id="nvda-momo-001")
    assert (rejected["status"], rejected["rule"]) == ("rejected", "market_data")
    assert "chain request timed out" in rejected["reason"]
    assert h.notifications("Rejected: NVDA bull_call_spread")
    assert h.broker.orders == {}

    h.submit(trade_signal("nvda-momo-002"))
    h.fill_entry("nvda-momo-002")


def test_reconcile_reports_differences_with_the_broker(h):
    assert h.trader.reconcile() == {}
    h.submit(trade_signal())
    pos = h.fill_entry()
    assert h.trader.reconcile() == {}  # store and broker agree

    long_leg, short_leg = json.loads(pos["legs"])
    (order,) = h.broker.orders.values()
    order.filled = 0  # the broker lost the fill
    diff = h.trader.reconcile()
    assert diff == {long_leg["con_id"]: (1, 0), short_leg["con_id"]: (-1, 0)}


def test_reconcile_reports_positions_the_store_does_not_know(h):
    h.broker.fill_mode = "always"
    from helpers_trading import quote, selection

    sel = selection(quote(100, con_id=777))
    order_id = h.broker.submit(sel.legs, "BUY", 3, 1.0, "manual|x|v1|int_x")
    h.broker.poll(order_id)
    assert h.trader.reconcile() == {777: (0.0, 3)}


def test_scorecard_after_a_win_and_a_loss(h):
    h.submit(trade_signal("nvda-win-001"))
    h.submit(trade_signal("amd-loss-001", underlying="AMD"))
    h.submit("this is not a signal", "momentum-agent")  # rejected raw payload
    h.submit("this is not a signal either", "macro-agent")
    h.fill_entry("nvda-win-001")
    h.fill_entry("amd-loss-001")

    h.md.set_spot("NVDA", 195.0)
    h.md.set_spot("AMD", 90.0)
    h.run_until(lambda: not h.store.open_positions())

    card = {c["producer"]: c for c in h.store.scorecard()}
    momentum = card["momentum-agent"]
    positions = {p["underlying"]: p for p in h.positions()}
    assert positions["NVDA"]["realized_pnl"] > 0
    assert positions["AMD"]["realized_pnl"] < 0
    assert momentum["received"] == 3
    assert momentum["accepted"] == 2
    assert momentum["rejected"] == 1
    assert momentum["trades"] == 2
    assert momentum["closed"] == 2
    assert momentum["wins"] == 1
    assert momentum["hit_rate"] == 0.5
    assert momentum["pnl"] == pytest.approx(
        positions["NVDA"]["realized_pnl"] + positions["AMD"]["realized_pnl"]
    )
    assert momentum["shadow_pnl"] == 0
    other = card["macro-agent"]  # a second producer's numbers are separate
    assert (other["received"], other["accepted"], other["rejected"]) == (1, 0, 1)
    assert (other["trades"], other["closed"], other["hit_rate"]) == (0, 0, None)


def test_daily_summary_is_sent_once_after_the_summary_time(h):
    h.at(utc(2026, 10, 19, 20, 14)).run(1)  # 16:14 New York
    assert h.notifications("Daily summary") == []
    h.at(utc(2026, 10, 19, 20, 15)).run(3)
    (summary,) = h.notifications("Daily summary")
    assert "Mode: paper" in summary[1]
    assert "Kill switch: off" in summary[1]


def test_every_step_decision_leaves_an_audit_trail(h):
    h.submit(trade_signal())
    h.fill_entry()
    close_position(h, spot=195.0)
    events = {row["event"] for row in h.store.query("SELECT event FROM audit_log")}
    assert {
        "signal_received",
        "signal_accepted",
        "risk_approved",
        "order_submitted",
        "order_fill",
        "position_opened",
        "exit_created",
        "position_closed",
    } <= events


# ==================================================================== exit safety
def test_unknown_exit_blocks_a_second_exit_until_reconciled(h):
    h.submit(trade_signal())
    pos = h.fill_entry()
    h.broker.fill_mode = "never"
    h.md.set_spot("NVDA", 195.0)  # take profit due
    h.run(1)
    (exit_intent,) = h.intents(kind="exit")
    h.store.update_intent(exit_intent["id"], h.now, status="unknown")  # e.g. after a restart
    h.run(5)
    assert len(h.intents(kind="exit")) == 1
    assert h.store.get_position(pos["id"])["status"] == "open"


def test_failed_exit_is_retried_only_after_backoff(h):
    from ibtrader.app import EXIT_RETRY_BACKOFF_SECONDS

    h.submit(trade_signal())
    h.fill_entry()
    working = h.trader.orders.brokers[False]
    h.trader.orders.brokers[False] = FailingBroker(h.md)
    h.md.set_spot("NVDA", 195.0)
    h.run(4)  # 100 s: one failed exit, no retry storm
    assert [i["status"] for i in h.intents(kind="exit")] == ["error"]

    h.trader.orders.brokers[False] = working
    h.at(h.now + timedelta(seconds=EXIT_RETRY_BACKOFF_SECONDS))
    close_position(h)
    statuses = [i["status"] for i in h.intents(kind="exit")]
    assert statuses == ["error", "filled"]
