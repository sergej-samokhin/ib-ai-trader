"""Trader: wires gateway, evaluator, selector, risk engine, order manager and monitor.

Single-threaded by design: `tick()` runs every LOOP_INTERVAL_SECONDS and walks the lifecycle
(spec section 5). Transports may call `gateway.submit` from their own thread; the store is
locked, and the gateway only writes signals that `tick()` picks up later.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

from pydantic import TypeAdapter

from ibtrader.audit import Audit
from ibtrader.clock import NY, in_entry_window, is_market_open, is_trading_day, ny_date, to_ny
from ibtrader.config import Settings
from ibtrader.contract.models import EntrySignal, RuleSignal, Signal, Strategy
from ibtrader.domain import close_prices, legs_from_json, legs_to_json, net_prices
from ibtrader.evaluator import RuleEvaluator
from ibtrader.gateway import SignalGateway
from ibtrader.killswitch import KillSwitch
from ibtrader.marketdata import MarketData
from ibtrader.monitor import exit_reason
from ibtrader.notify import Notifier
from ibtrader.orders import Broker, OrderManager
from ibtrader.producers import ProducerRegistry, ProducerStatus
from ibtrader.risk import EntryRequest, PortfolioSnapshot, RiskEngine
from ibtrader.selector import SelectionError, select
from ibtrader.sim import SimBroker
from ibtrader.store import OPEN_INTENT_STATUSES, Store, iso, parse_iso

log = logging.getLogger("ibtrader.app")
_SIGNAL = TypeAdapter(Signal)
FeedbackSink = Callable[[str, dict], None]
BODY_PREVIEW_CHARS = 1500


class Trader:
    def __init__(
        self,
        settings: Settings,
        registry: ProducerRegistry,
        store: Store,
        md: MarketData,
        broker: Broker,
        notifier: Notifier,
        shadow_broker: Broker | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.md = md
        self.broker = broker
        self.notifier = notifier
        self.audit = Audit(store)
        self.kill_switch = KillSwitch(settings.kill_switch_file)
        self.gateway = SignalGateway(settings, registry, store, self.audit, notifier)
        self.risk = RiskEngine(settings.risk, self.kill_switch)
        self.evaluator = RuleEvaluator(md, settings.rule_eval_interval_seconds)
        self.orders = OrderManager(
            store,
            md,
            {False: broker, True: shadow_broker or SimBroker(md)},
            self.audit,
            notifier,
            settings.price_tick,
            settings.reprice_interval_seconds,
        )
        self.feedback_sinks: list[FeedbackSink] = []

    # ============================================================================ loop
    def tick(self, now: datetime) -> None:
        for step in (
            self._kill_switch_step,
            self._expire_signals,
            self._process_trades,
            self._process_exit_signals,
            self._evaluate_rules,
            self._process_approvals,
            self._manage_orders,
            self._monitor_positions,
            self._daily_summary,
        ):
            try:
                step(now)
            except Exception as exc:  # noqa: BLE001 - one failing step must not stop the loop
                log.exception("tick step failed", extra={"fields": {"step": step.__name__}})
                self.audit("error", now, level=logging.ERROR, step=step.__name__, error=str(exc))
                self.notifier.send("Trader error", f"{step.__name__}: {exc}", "error")

    # ============================================================================ helpers
    def _feedback(self, producer: str, payload: dict) -> None:
        for sink in self.feedback_sinks:
            try:
                sink(producer, payload)
            except Exception:  # noqa: BLE001
                log.exception("feedback sink failed")

    @staticmethod
    def _model(row) -> object:
        return _SIGNAL.validate_json(row["data"])

    def snapshot(self, now: datetime, shadow: bool) -> PortfolioSnapshot:
        by_producer: dict[str, float] = {}
        by_underlying: dict[str, float] = {}
        total = 0.0
        count = 0
        for pos in self.store.open_positions(shadow=shadow):
            risk = pos["entry_price"] * 100 * pos["quantity"]
            count += 1
            total += risk
            by_producer[pos["producer"]] = by_producer.get(pos["producer"], 0.0) + risk
            by_underlying[pos["underlying"]] = by_underlying.get(pos["underlying"], 0.0) + risk
        for intent in self.store.intents_with_status(*OPEN_INTENT_STATUSES):
            if intent["kind"] != "entry" or bool(intent["shadow"]) != shadow:
                continue
            risk = (intent["max_price"] or 0.0) * 100 * intent["quantity"]
            count += 1
            total += risk
            by_producer[intent["producer"]] = by_producer.get(intent["producer"], 0.0) + risk
            by_underlying[intent["underlying"]] = (
                by_underlying.get(intent["underlying"], 0.0) + risk
            )
        midnight = datetime.combine(ny_date(now), time(0), NY)
        return PortfolioSnapshot(
            open_positions=count,
            premium_at_risk=total,
            by_producer=by_producer,
            by_underlying=by_underlying,
            realized_pnl_today=self.store.realized_pnl_since(midnight, shadow=shadow),
        )

    # ============================================================================ steps
    def _kill_switch_step(self, now: datetime) -> None:
        active = self.kill_switch.is_active()
        was_active = self.store.get_kv("kill_switch_active") == "1"
        if active and not was_active:
            cancelled = 0
            for intent in self.store.intents_with_status(*OPEN_INTENT_STATUSES):
                if intent["kind"] != "entry":
                    continue  # exits are always allowed
                if intent["status"] == "submitted":
                    self.orders.cancel(intent, now, "kill switch")
                else:
                    self.store.update_intent(
                        intent["id"],
                        now,
                        status="cancelled",
                        rule="kill_switch",
                        reason="kill switch",
                    )
                cancelled += 1
            self.store.set_kv("kill_switch_active", "1")
            self.audit(
                "kill_switch_activated",
                now,
                level=logging.WARNING,
                reason=self.kill_switch.reason(),
                entries_cancelled=cancelled,
            )
            self.notifier.send(
                "KILL SWITCH ACTIVE",
                f"New entries blocked; {cancelled} open entry order(s) cancelled. Exits continue.",
                "critical",
            )
        elif not active and was_active:
            self.store.set_kv("kill_switch_active", "0")
            self.audit("kill_switch_deactivated", now, level=logging.WARNING)
            self.notifier.send("Kill switch released", "New entries are allowed again.", "warning")

    def _expire_signals(self, now: datetime) -> None:
        for row in self.store.signals_with_status("pending", "active"):
            if parse_iso(row["valid_until"]) <= now:
                self.store.set_signal_status(
                    row["producer"], row["signal_id"], row["version"], "expired", "valid_until", now
                )
                self.audit(
                    "signal_expired",
                    now,
                    producer=row["producer"],
                    signal_id=row["signal_id"],
                    version=row["version"],
                )
                self._feedback(
                    row["producer"],
                    {"signal_id": row["signal_id"], "version": row["version"], "event": "expired"},
                )

    def _process_trades(self, now: datetime) -> None:
        lim = self.settings.risk
        waiting_for_session = not in_entry_window(
            now, lim.no_entry_open_minutes, lim.no_entry_close_minutes
        )
        for row in self.store.signals_with_status("pending", kind="trade"):
            if waiting_for_session and not self.kill_switch.is_active():
                continue  # keep pending until the entry window opens (or the signal expires)
            self.store.set_signal_status(
                row["producer"], row["signal_id"], row["version"], "consumed", "intent created", now
            )
            self.create_entry(row, self._model(row), now)

    def _process_exit_signals(self, now: datetime) -> None:
        for row in self.store.signals_with_status("pending", kind="exit"):
            sig = self._model(row)
            producer, target = row["producer"], sig.target_signal_id
            acted = 0
            for intent in self.store.intents_with_status(*OPEN_INTENT_STATUSES):
                if (intent["producer"], intent["signal_id"], intent["kind"]) == (
                    producer,
                    target,
                    "entry",
                ):
                    if intent["status"] == "submitted":
                        self.orders.cancel(intent, now, f"exit signal {sig.signal_id}")
                    else:
                        self.store.update_intent(
                            intent["id"],
                            now,
                            status="cancelled",
                            reason=f"exit signal {sig.signal_id}",
                        )
                    acted += 1
            for pos in self.store.open_positions():
                if pos["producer"] == producer and pos["signal_id"] == target:
                    qty = self.risk.exit_quantity(pos["quantity"], sig.action, sig.reduce_pct)
                    if self.create_exit(pos, qty, f"exit_signal:{sig.signal_id}", now):
                        acted += 1
            outcome = "consumed" if acted else "no_position"
            self.store.set_signal_status(
                producer, sig.signal_id, sig.version, outcome, f"{acted} action(s)", now
            )
            self._feedback(
                producer,
                {
                    "signal_id": sig.signal_id,
                    "version": sig.version,
                    "event": outcome,
                    "actions": acted,
                },
            )

    def _evaluate_rules(self, now: datetime) -> None:
        lim = self.settings.risk
        in_window = in_entry_window(now, lim.no_entry_open_minutes, lim.no_entry_close_minutes)
        for row in self.store.signals_with_status("active", kind="rule"):
            rule: RuleSignal = self._model(row)  # type: ignore[assignment]
            if not rule.trigger.check_at and not in_window:
                continue
            if not is_trading_day(ny_date(now)) or not self.evaluator.is_due(row, rule, now):
                continue
            fired, values = self.evaluator.evaluate(rule)
            today = ny_date(now).isoformat()
            self.store.update_rule_eval(
                row["producer"], row["signal_id"], row["version"], now, today
            )
            self.audit(
                "rule_evaluated",
                now,
                producer=row["producer"],
                signal_id=row["signal_id"],
                version=row["version"],
                fired=fired,
                values=values,
            )
            if fired:
                self.store.bump_rule_entries(
                    row["producer"], row["signal_id"], row["version"], today
                )
                self.create_entry(row, rule, now)

    def _process_approvals(self, now: datetime) -> None:
        for intent in self.store.intents_with_status("awaiting_approval"):
            deadline = intent["approval_deadline"]
            if deadline and parse_iso(deadline) <= now:
                self.store.update_intent(
                    intent["id"], now, status="expired", reason="approval timeout"
                )
                self._intent_event("approval_expired", intent, now)
                self.notifier.send("Approval expired", f"{intent['id']} was not approved in time.")
        for intent in self.store.intents_with_status("approved"):
            if intent["kind"] == "entry" and not self._refresh_entry(intent, now):
                continue
            self.orders.submit(self.store.get_intent(intent["id"]), now)

    def _refresh_entry(self, intent, now: datetime) -> bool:
        """Last checks right before an entry order goes out (prices may have moved)."""
        lim = self.settings.risk
        reject_rule = None
        if self.kill_switch.is_active():
            reject_rule, reason = "kill_switch", "kill switch is active"
        elif not in_entry_window(now, lim.no_entry_open_minutes, lim.no_entry_close_minutes):
            reject_rule, reason = "market_hours", "outside the entry window"
        else:
            legs = legs_from_json(intent["legs"])
            mid, natural = net_prices(legs, self.md.quote_legs(legs))
            if mid is None or mid > intent["max_price"]:
                reject_rule, reason = (
                    "price_moved",
                    f"mid {mid} above max price {intent['max_price']}",
                )
            else:
                self.store.update_intent(
                    intent["id"], now, limit_price=mid, mid=mid, natural=natural
                )
        if reject_rule:
            self.store.update_intent(
                intent["id"], now, status="rejected", rule=reject_rule, reason=reason
            )
            self._intent_event("risk_rejected", intent, now, rule=reject_rule, reason=reason)
            self._feedback(
                intent["producer"],
                {
                    "signal_id": intent["signal_id"],
                    "version": intent["version"],
                    "event": "rejected",
                    "rule": reject_rule,
                    "reason": reason,
                    "intent_id": intent["id"],
                },
            )
            return False
        return True

    def _manage_orders(self, now: datetime) -> None:
        for intent in self.store.intents_with_status("submitted"):
            result = self.orders.step(intent, now)
            if result is None:
                continue
            final = self.store.get_intent(intent["id"])
            if result == "filled":
                if final["kind"] == "entry":
                    self._on_entry_fill(final, now)
                else:
                    self._on_exit_fill(final, now)
            self._feedback(
                final["producer"],
                {
                    "signal_id": final["signal_id"],
                    "version": final["version"],
                    "event": f"order_{final['status']}",
                    "intent_id": final["id"],
                    "filled_qty": final["filled_qty"],
                },
            )

    def _on_entry_fill(self, intent, now: datetime) -> None:
        row = self.store.get_signal(intent["producer"], intent["signal_id"], intent["version"])
        data = json.loads(row["data"])
        position_id = self.store.insert_position(
            now,
            intent_id=intent["id"],
            producer=intent["producer"],
            signal_id=intent["signal_id"],
            version=intent["version"],
            underlying=intent["underlying"],
            strategy=intent["strategy"],
            legs=intent["legs"],
            quantity=intent["filled_qty"],
            entry_price=intent["avg_fill_price"],
            exit_rules=json.dumps(data["exit"]),
            max_reprice_steps=data["order"]["max_reprice_steps"],
            shadow=intent["shadow"],
        )
        self.store.update_intent(intent["id"], now, position_id=position_id)
        self._intent_event(
            "position_opened",
            intent,
            now,
            position_id=position_id,
            quantity=intent["filled_qty"],
            entry_price=intent["avg_fill_price"],
        )
        tag = " [SHADOW]" if intent["shadow"] else ""
        self.notifier.send(
            f"Filled{tag}: {intent['underlying']} {intent['strategy']}",
            f"{intent['filled_qty']} @ {intent['avg_fill_price']:.2f} "
            f"({intent['producer']}/{intent['signal_id']} v{intent['version']})",
        )

    def _on_exit_fill(self, intent, now: datetime) -> None:
        pnl = self.store.record_close(
            intent["position_id"], intent["filled_qty"], intent["avg_fill_price"], now
        )
        self._intent_event(
            "position_closed",
            intent,
            now,
            position_id=intent["position_id"],
            quantity=intent["filled_qty"],
            exit_price=intent["avg_fill_price"],
            realized_pnl=round(pnl, 2),
            exit_reason=intent["exit_reason"],
        )
        tag = " [SHADOW]" if intent["shadow"] else ""
        self.notifier.send(
            f"Exit filled{tag}: {intent['underlying']}",
            f"{intent['filled_qty']} @ {intent['avg_fill_price']:.2f}, P&L {pnl:+.2f} USD "
            f"({intent['exit_reason']})",
        )

    def _monitor_positions(self, now: datetime) -> None:
        if not is_market_open(now):
            return
        today = ny_date(now)
        for pos in self.store.open_positions():
            if self.store.open_intents_for_position(pos["id"]):
                continue
            quotes = self.md.quote_legs(legs_from_json(pos["legs"]))
            reason, pnl_pct = exit_reason(pos, quotes, today)
            if reason:
                self.create_exit(pos, pos["quantity"], reason, now, pnl_pct=pnl_pct)

    def _daily_summary(self, now: datetime) -> None:
        ny_now = to_ny(now)
        hour, minute = (int(x) for x in self.settings.daily_summary_time.split(":"))
        today = ny_now.date().isoformat()
        if ny_now.time() < time(hour, minute) or self.store.get_kv("summary_date") == today:
            return
        if not is_trading_day(ny_now.date()):
            return
        self.store.set_kv("summary_date", today)
        self.notifier.send(f"Daily summary {today}", self.summary_text(now))

    # ============================================================================ actions
    def create_entry(self, row, sig: EntrySignal, now: datetime) -> str:
        producer = self.registry.get(row["producer"])
        shadow = bool(
            producer and self.settings.is_live and producer.status == ProducerStatus.PAPER_ONLY
        )
        base = dict(
            producer=row["producer"],
            signal_id=row["signal_id"],
            version=row["version"],
            kind="entry",
            underlying=sig.underlying,
            strategy=str(sig.strategy),
            shadow=int(shadow),
            max_reprice_steps=sig.order.max_reprice_steps,
        )
        if producer is None:
            return self._rejected_intent(
                base, "unknown_producer", "producer left the registry", now
            )
        try:
            selection = select(
                Strategy(sig.strategy),
                sig.contract,
                self.md,
                sig.underlying,
                ny_date(now),
                self.settings.risk.max_delta_deviation,
            )
        except SelectionError as exc:
            return self._rejected_intent(base, "contract_selection", str(exc), now)
        except Exception as exc:  # noqa: BLE001 - feed outage: reject loudly, never drop silently
            log.exception("market data error during contract selection")
            return self._rejected_intent(base, "market_data", f"market data error: {exc}", now)

        request = EntryRequest(
            producer=producer,
            producer_paused=self.store.is_producer_paused(producer.id),
            underlying=sig.underlying,
            strategy=str(sig.strategy),
            order_type=sig.order.type,
            max_premium_usd=sig.sizing.max_premium_usd,
            max_contracts=sig.sizing.max_contracts,
            confidence=sig.confidence,
            selection=selection,
            now=now,
            shadow=shadow,
        )
        decision = self.risk.check_entry(request, self.snapshot(now, shadow))
        base.update(legs=legs_to_json(selection.legs), mid=selection.mid, natural=selection.natural)
        if not decision.approved:
            return self._rejected_intent(base, decision.rule or "risk", decision.reason, now)

        status = "awaiting_approval" if decision.requires_approval else "approved"
        deadline = (
            iso(now + timedelta(minutes=self.settings.risk.approval_timeout_minutes))
            if decision.requires_approval
            else None
        )
        intent_id = self.store.insert_intent(
            now,
            **base,
            quantity=decision.quantity,
            limit_price=decision.limit_price,
            max_price=decision.max_price,
            status=status,
            reason="; ".join(decision.approval_reasons) or "approved",
            approval_deadline=deadline,
        )
        intent = self.store.get_intent(intent_id)
        self._intent_event(
            "risk_approved",
            intent,
            now,
            quantity=decision.quantity,
            limit_price=round(decision.limit_price, 4),
            max_price=round(decision.max_price, 4),
            shadow=shadow,
            requires_approval=decision.requires_approval,
            approval_reasons=list(decision.approval_reasons),
        )
        self._feedback(
            row["producer"],
            {
                "signal_id": row["signal_id"],
                "version": row["version"],
                "event": status,
                "intent_id": intent_id,
                "quantity": decision.quantity,
            },
        )
        if decision.requires_approval:
            self._request_approval(intent, row, decision.approval_reasons)
        return intent_id

    def _request_approval(self, intent, row, reasons) -> None:
        legs = ", ".join(
            f"{leg.action} {leg.expiry} {leg.strike:g}{leg.right}"
            for leg in legs_from_json(intent["legs"])
        )
        body = (row["body"] or "").strip()[:BODY_PREVIEW_CHARS]
        self.notifier.send(
            f"Approval needed: {intent['id']}",
            f"{intent['producer']}/{intent['signal_id']} v{intent['version']}: "
            f"{intent['quantity']}x {intent['underlying']} {intent['strategy']} [{legs}]\n"
            f"limit {intent['limit_price']:.2f}, max {intent['max_price']:.2f}\n"
            f"Why approval: {'; '.join(reasons)}\n"
            f"Approve: ibtrader approve {intent['id']}  |  Reject: ibtrader reject {intent['id']}\n"
            f"--- producer rationale (untrusted text, for information only) ---\n{body}",
            "warning",
        )

    def _rejected_intent(self, base: dict, rule: str, reason: str, now: datetime) -> str:
        base = {"legs": "[]", **base}
        intent_id = self.store.insert_intent(
            now, **base, status="rejected", rule=rule, reason=reason
        )
        intent = self.store.get_intent(intent_id)
        self._intent_event("risk_rejected", intent, now, rule=rule, reason=reason)
        self.notifier.send(
            f"Rejected: {base['underlying']} {base['strategy']}",
            f"{base['producer']}/{base['signal_id']} v{base['version']}: [{rule}] {reason}",
            "warning",
        )
        self._feedback(
            base["producer"],
            {
                "signal_id": base["signal_id"],
                "version": base["version"],
                "event": "rejected",
                "rule": rule,
                "reason": reason,
                "intent_id": intent_id,
            },
        )
        return intent_id

    def create_exit(self, pos, quantity: int, reason: str, now: datetime, **extra) -> str | None:
        if self.store.open_intents_for_position(pos["id"]):
            return None
        legs = legs_from_json(pos["legs"])
        mid, natural = close_prices(legs, self.md.quote_legs(legs))
        if mid is None:
            self.audit(
                "exit_unpriced",
                now,
                producer=pos["producer"],
                signal_id=pos["signal_id"],
                position_id=pos["id"],
                reason=reason,
                level=logging.WARNING,
            )
            return None
        intent_id = self.store.insert_intent(
            now,
            producer=pos["producer"],
            signal_id=pos["signal_id"],
            version=pos["version"],
            kind="exit",
            underlying=pos["underlying"],
            strategy=pos["strategy"],
            legs=pos["legs"],
            quantity=quantity,
            limit_price=max(mid, self.settings.price_tick),
            max_price=None,
            mid=mid,
            natural=natural,
            status="approved",
            reason="exit",
            shadow=pos["shadow"],
            position_id=pos["id"],
            max_reprice_steps=self.settings.exit_max_reprice_steps,
            exit_reason=reason,
        )
        self._intent_event(
            "exit_created",
            self.store.get_intent(intent_id),
            now,
            reason=reason,
            position_id=pos["id"],
            quantity=quantity,
            **extra,
        )
        return intent_id

    def approve(self, intent_id: str, who: str, now: datetime) -> bool:
        intent = self.store.get_intent(intent_id)
        if intent is None or intent["status"] != "awaiting_approval":
            return False
        self.store.update_intent(intent_id, now, status="approved", approved_by=who)
        self._intent_event("manual_approved", intent, now, approved_by=who)
        return True

    def reject(self, intent_id: str, who: str, now: datetime) -> bool:
        intent = self.store.get_intent(intent_id)
        if intent is None or intent["status"] != "awaiting_approval":
            return False
        self.store.update_intent(
            intent_id, now, status="rejected", rule="manual", reason=f"rejected by {who}"
        )
        self._intent_event("manual_rejected", intent, now, rejected_by=who)
        self._feedback(
            intent["producer"],
            {
                "signal_id": intent["signal_id"],
                "version": intent["version"],
                "event": "rejected",
                "rule": "manual",
                "intent_id": intent_id,
            },
        )
        return True

    # ============================================================================ ops
    def recover(self, now: datetime) -> None:
        """After a restart: re-attach working orders by orderRef, flag the rest."""
        for intent in self.store.intents_with_status("submitted"):
            if intent["shadow"]:
                self.store.update_intent(
                    intent["id"], now, status="cancelled", reason="simulated order lost on restart"
                )
                continue
            order_id = self.broker.find_by_ref(intent["order_ref"]) if intent["order_ref"] else None
            if order_id:
                self.store.update_intent(intent["id"], now, broker_order_id=order_id)
                self._intent_event("order_reattached", intent, now, order_id=order_id)
            else:
                self.store.update_intent(
                    intent["id"],
                    now,
                    status="unknown",
                    reason="order not found at broker after restart",
                )
                self._intent_event("order_unknown", intent, now, level=logging.WARNING)
                self.notifier.send(
                    "Order state unknown after restart",
                    f"{intent['id']} ({intent['order_ref']}): reconcile with IBKR manually.",
                    "error",
                )

    def reconcile(self) -> dict[int, tuple[float, float]]:
        """conId -> (expected from the store, actual at the broker) where they differ."""
        expected: dict[int, float] = {}
        for pos in self.store.open_positions(shadow=False):
            for leg in legs_from_json(pos["legs"]):
                sign = 1 if leg.action == "BUY" else -1
                key = leg.con_id or 0
                expected[key] = expected.get(key, 0) + sign * pos["quantity"] * leg.ratio
        actual = self.broker.positions()
        keys = set(expected) | set(actual)
        return {
            k: (expected.get(k, 0.0), actual.get(k, 0.0))
            for k in keys
            if abs(expected.get(k, 0.0) - actual.get(k, 0.0)) > 1e-9
        }

    def summary_text(self, now: datetime) -> str:
        midnight = datetime.combine(ny_date(now), time(0), NY)
        pnl = self.store.realized_pnl_since(midnight)
        shadow_pnl = self.store.realized_pnl_since(midnight, shadow=True)
        open_pos = self.store.open_positions(shadow=False)
        risk = sum(p["entry_price"] * 100 * p["quantity"] for p in open_pos)
        intents = self.store.query(
            "SELECT status, COUNT(*) AS n FROM intents WHERE created_at >= ? GROUP BY status",
            (iso(midnight),),
        )
        counts = ", ".join(f"{r['status']}={r['n']}" for r in intents) or "none"
        return (
            f"Mode: {self.settings.mode}\n"
            f"Realised P&L today: {pnl:+.2f} USD (shadow {shadow_pnl:+.2f})\n"
            f"Open positions: {len(open_pos)}, premium at risk {risk:.2f} USD\n"
            f"Intents today: {counts}\n"
            f"Kill switch: {'ACTIVE' if self.kill_switch.is_active() else 'off'}"
        )

    def _intent_event(self, event: str, intent, now: datetime, level: int = logging.INFO, **data):
        self.audit(
            event,
            now,
            producer=intent["producer"],
            signal_id=intent["signal_id"],
            version=intent["version"],
            intent_id=intent["id"],
            level=level,
            **data,
        )
