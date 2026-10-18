"""Order Manager: limit orders at mid, bounded repricing, partial fills, cancels, exits.

Spreads always go out as one combo (BAG) order; the broker adapter takes care of that.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ibtrader.audit import Audit
from ibtrader.domain import Leg, close_prices, legs_from_json, net_prices
from ibtrader.marketdata import MarketData
from ibtrader.notify import Notifier
from ibtrader.store import Store, parse_iso

BrokerState = Literal["working", "filled", "cancelled", "rejected"]


@dataclass(frozen=True)
class BrokerOrderStatus:
    state: BrokerState
    filled_qty: int = 0
    avg_fill_price: float | None = None  # per share, net (combo price for spreads)
    message: str = ""


class Broker(Protocol):
    def submit(
        self, legs: list[Leg], action: str, quantity: int, limit_price: float, order_ref: str
    ) -> str:
        """Place a limit order (combo if len(legs) > 1). Returns the broker order id."""

    def modify(self, order_id: str, limit_price: float) -> None: ...

    def cancel(self, order_id: str) -> None: ...

    def poll(self, order_id: str) -> BrokerOrderStatus: ...

    def find_by_ref(self, order_ref: str) -> str | None:
        """Re-attach to a working order after a restart."""

    def positions(self) -> dict[int, float]:
        """conId -> signed quantity, for reconciliation."""


def order_ref(producer: str, signal_id: str, version: int, intent_id: str) -> str:
    return f"{producer}|{signal_id}|v{version}|{intent_id}"


def round_buy(price: float, tick: float) -> float:
    """Round a buy limit down to the tick (never pay more than computed)."""
    return max(tick, round(math.floor(price / tick + 1e-9) * tick, 4))


def round_sell(price: float, tick: float) -> float:
    """Round a sell limit up to the tick (never sell for less than computed)."""
    return max(tick, round(math.ceil(price / tick - 1e-9) * tick, 4))


def ladder(mid: float, natural: float, step: int, steps: int) -> float:
    """Price for reprice step `step` of `steps`, walking linearly from mid to natural."""
    if steps <= 0:
        return mid
    return mid + (natural - mid) * min(step, steps) / steps


class OrderManager:
    def __init__(
        self,
        store: Store,
        md: MarketData,
        brokers: dict[bool, Broker],  # shadow flag -> broker
        audit: Audit,
        notifier: Notifier,
        tick: float,
        reprice_interval_seconds: float,
    ):
        self.store = store
        self.md = md
        self.brokers = brokers
        self.audit = audit
        self.notifier = notifier
        self.tick = tick
        self.reprice_interval = reprice_interval_seconds

    def broker_for(self, intent) -> Broker:
        return self.brokers[bool(intent["shadow"])]

    # -----------------------------------------------------------------------------
    def submit(self, intent, now: datetime) -> None:
        legs = legs_from_json(intent["legs"])
        is_entry = intent["kind"] == "entry"
        price = (round_buy if is_entry else round_sell)(intent["limit_price"], self.tick)
        ref = order_ref(intent["producer"], intent["signal_id"], intent["version"], intent["id"])
        action = "BUY" if is_entry else "SELL"
        try:
            order_id = self.broker_for(intent).submit(legs, action, intent["quantity"], price, ref)
        except Exception as exc:  # noqa: BLE001
            self.store.update_intent(intent["id"], now, status="error", reason=str(exc))
            self._event("order_error", intent, now, error=str(exc))
            self.notifier.send("Order submission failed", f"{intent['id']}: {exc}", "error")
            return
        self.store.update_intent(
            intent["id"],
            now,
            status="submitted",
            broker_order_id=order_id,
            order_ref=ref,
            limit_price=price,
            last_action_at=now.isoformat(),
        )
        self._event(
            "order_submitted",
            intent,
            now,
            order_id=order_id,
            price=price,
            quantity=intent["quantity"],
            action=action,
            shadow=bool(intent["shadow"]),
        )

    def cancel(self, intent, now: datetime, reason: str) -> None:
        if intent["broker_order_id"]:
            try:
                self.broker_for(intent).cancel(intent["broker_order_id"])
            except Exception as exc:  # noqa: BLE001
                self._event("order_error", intent, now, error=f"cancel failed: {exc}")
        self.store.update_intent(intent["id"], now, reason=reason, last_action_at=now.isoformat())
        self._event("order_cancel_requested", intent, now, reason=reason)

    def step(self, intent, now: datetime) -> str | None:
        """Advance one working order. Returns 'filled' / 'done' / None (still working)."""
        broker = self.broker_for(intent)
        status = broker.poll(intent["broker_order_id"])
        if status.filled_qty != intent["filled_qty"]:
            self.store.update_intent(
                intent["id"],
                now,
                filled_qty=status.filled_qty,
                avg_fill_price=status.avg_fill_price,
            )
            self._event(
                "order_fill",
                intent,
                now,
                filled_qty=status.filled_qty,
                avg_fill_price=status.avg_fill_price,
            )

        if status.state in ("filled", "cancelled", "rejected"):
            final = (
                "filled"
                if status.state == "filled"
                else ("partially_filled" if status.filled_qty > 0 else status.state)
            )
            self.store.update_intent(
                intent["id"], now, status=final, reason=status.message or intent["reason"]
            )
            self._event(
                f"order_{final}", intent, now, filled_qty=status.filled_qty, message=status.message
            )
            return "filled" if status.filled_qty > 0 else "done"

        last = parse_iso(intent["last_action_at"]) if intent["last_action_at"] else None
        if last is not None and (now - last).total_seconds() < self.reprice_interval:
            return None
        self._reprice(intent, broker, now)
        return None

    def _reprice(self, intent, broker: Broker, now: datetime) -> None:
        is_entry = intent["kind"] == "entry"
        steps = intent["max_reprice_steps"]
        step = intent["reprice_step"]
        if step >= steps and is_entry:
            # Ladder exhausted: cancel the remainder. Exits keep working at natural.
            self.cancel(intent, now, "reprice steps exhausted")
            return
        legs = legs_from_json(intent["legs"])
        quotes = self.md.quote_legs(legs)
        mid, natural = (net_prices if is_entry else close_prices)(legs, quotes)
        if mid is None or natural is None:
            self.store.update_intent(intent["id"], now, last_action_at=now.isoformat())
            return
        step = min(step + 1, steps)
        if is_entry:
            price = round_buy(
                min(ladder(mid, natural, step, steps), intent["max_price"]), self.tick
            )
        else:
            target = ladder(mid, natural, step, steps) if steps else natural
            # Intermediate steps round up (patient); the final step must be marketable, so it
            # rounds DOWN to the tick at or below the natural price. Rounding it up would park
            # the order up to one tick above the bid, where it may never fill.
            rounder = round_buy if step >= steps else round_sell
            price = rounder(max(target, 0.0), self.tick)
        if price != intent["limit_price"]:
            broker.modify(intent["broker_order_id"], price)
        self.store.update_intent(
            intent["id"], now, reprice_step=step, limit_price=price, last_action_at=now.isoformat()
        )
        self._event(
            "order_repriced",
            intent,
            now,
            step=step,
            of=steps,
            price=price,
            mid=round(mid, 4),
            natural=round(natural, 4),
        )
        if not is_entry and intent["reprice_step"] < steps <= step:
            self.notifier.send(
                "Exit not yet filled",
                f"{intent['id']} ({intent['underlying']}) is working at the natural price {price}",
                "warning",
            )

    def _event(self, event: str, intent, now: datetime, **data) -> None:
        self.audit(
            event,
            now,
            producer=intent["producer"],
            signal_id=intent["signal_id"],
            version=intent["version"],
            intent_id=intent["id"],
            **data,
        )


def exit_rules_json(rules: dict) -> str:
    return json.dumps(rules)
