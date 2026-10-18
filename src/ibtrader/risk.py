"""Risk Engine: the single point of veto. Global limits always win over signal requests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from ibtrader.clock import in_entry_window
from ibtrader.config import RiskLimits
from ibtrader.contract.models import ALLOWED_STRATEGIES, Strategy
from ibtrader.killswitch import KillSwitch
from ibtrader.producers import Producer, ProducerStatus
from ibtrader.selector import Selection

# Rule identifiers (logged with every rejection)
KILL_SWITCH = "kill_switch"
PRODUCER_PAUSED = "producer_paused"
STRATEGY_NOT_ALLOWED = "strategy_not_allowed"
ORDER_TYPE = "order_type"
MARKET_HOURS = "market_hours"
DAILY_LOSS = "daily_loss_limit"
MAX_OPEN_POSITIONS = "max_open_positions"
LIQUIDITY_OI = "liquidity_open_interest"
LIQUIDITY_SPREAD = "liquidity_spread"
MAX_POSITION = "max_position_premium"
MAX_PORTFOLIO = "max_portfolio_risk"
PRODUCER_BUDGET = "producer_budget"
UNDERLYING = "underlying_concentration"
SIGNAL_SIZING = "signal_max_contracts"


@dataclass(frozen=True)
class PortfolioSnapshot:
    open_positions: int = 0
    premium_at_risk: float = 0.0
    by_producer: dict[str, float] = field(default_factory=dict)
    by_underlying: dict[str, float] = field(default_factory=dict)
    realized_pnl_today: float = 0.0


@dataclass(frozen=True)
class EntryRequest:
    producer: Producer
    producer_paused: bool
    underlying: str
    strategy: str
    order_type: str
    max_premium_usd: float
    max_contracts: int
    confidence: float
    selection: Selection
    now: datetime
    shadow: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    rule: str | None = None
    reason: str = ""
    quantity: int = 0
    limit_price: float = 0.0
    max_price: float = 0.0  # per share; the order manager never reprices above this
    requires_approval: bool = False
    approval_reasons: tuple[str, ...] = ()

    @classmethod
    def reject(cls, rule: str, reason: str) -> RiskDecision:
        return cls(False, rule, reason)


class RiskEngine:
    def __init__(self, limits: RiskLimits, kill_switch: KillSwitch):
        self.limits = limits
        self.kill_switch = kill_switch

    def check_entry(self, req: EntryRequest, pf: PortfolioSnapshot) -> RiskDecision:
        lim = self.limits
        if self.kill_switch.is_active():
            return RiskDecision.reject(KILL_SWITCH, "kill switch is active")
        if req.producer.status == ProducerStatus.PAUSED or req.producer_paused:
            return RiskDecision.reject(PRODUCER_PAUSED, f"producer {req.producer.id} is paused")
        if req.strategy not in {s.value for s in ALLOWED_STRATEGIES}:
            return RiskDecision.reject(STRATEGY_NOT_ALLOWED, f"strategy {req.strategy}")
        if req.order_type != "limit":
            return RiskDecision.reject(ORDER_TYPE, "only limit orders are allowed")
        if not in_entry_window(req.now, lim.no_entry_open_minutes, lim.no_entry_close_minutes):
            return RiskDecision.reject(MARKET_HOURS, "outside the entry window of the session")
        if pf.realized_pnl_today <= -lim.max_daily_loss_usd:
            return RiskDecision.reject(
                DAILY_LOSS,
                f"realised P&L today {pf.realized_pnl_today:.2f} <= -{lim.max_daily_loss_usd:.2f}",
            )
        if pf.open_positions >= lim.max_open_positions:
            return RiskDecision.reject(
                MAX_OPEN_POSITIONS,
                f"{pf.open_positions} open positions (max {lim.max_open_positions})",
            )

        for quote in req.selection.quotes:
            label = f"{quote.symbol} {quote.expiry} {quote.strike}{quote.right}"
            if quote.open_interest is None or quote.open_interest < lim.min_open_interest:
                return RiskDecision.reject(
                    LIQUIDITY_OI,
                    f"{label} open interest {quote.open_interest} < {lim.min_open_interest}",
                )
            spread = quote.spread_pct
            if spread is None or spread > lim.max_bid_ask_spread_pct:
                shown = "n/a" if spread is None else f"{spread:.1f}%"
                return RiskDecision.reject(
                    LIQUIDITY_SPREAD,
                    f"{label} bid-ask spread {shown} > {lim.max_bid_ask_spread_pct}%",
                )

        # -- sizing: size against the worst case (natural) so caps hold after repricing ---
        worst_per_contract = req.selection.natural * 100
        rooms = {
            MAX_POSITION: min(req.max_premium_usd, lim.max_position_usd),
            MAX_PORTFOLIO: lim.max_portfolio_risk_usd - pf.premium_at_risk,
            PRODUCER_BUDGET: req.producer.budget_usd - pf.by_producer.get(req.producer.id, 0.0),
            UNDERLYING: lim.max_underlying_exposure_usd - pf.by_underlying.get(req.underlying, 0.0),
        }
        binding_rule = min(rooms, key=rooms.__getitem__)
        allowed_usd = rooms[binding_rule]
        by_money = math.floor(allowed_usd / worst_per_contract) if allowed_usd > 0 else 0
        quantity = min(req.max_contracts, by_money)
        if quantity < 1:
            return RiskDecision.reject(
                binding_rule,
                f"one contract costs up to {worst_per_contract:.2f} USD but only "
                f"{max(allowed_usd, 0):.2f} USD is available under {binding_rule}",
            )
        max_price = min(req.selection.natural, allowed_usd / quantity / 100)

        reasons: list[str] = []
        if not req.shadow:
            if req.producer.status == ProducerStatus.MANUAL:
                reasons.append("producer requires manual approval")
            premium = quantity * max_price * 100
            if premium > lim.manual_approval_above_usd:
                reasons.append(
                    f"premium up to {premium:.2f} USD > {lim.manual_approval_above_usd:.2f} USD"
                )
            if req.confidence < lim.min_confidence_auto:
                reasons.append(f"confidence {req.confidence} < {lim.min_confidence_auto}")

        return RiskDecision(
            approved=True,
            quantity=quantity,
            limit_price=min(req.selection.mid, max_price),
            max_price=max_price,
            requires_approval=bool(reasons),
            approval_reasons=tuple(reasons),
            reason="approved",
        )

    @staticmethod
    def exit_quantity(position_qty: int, action: str, reduce_pct: int) -> int:
        """Exits are always allowed (even with the kill switch); this only sizes them."""
        if action == "close" or reduce_pct >= 100:
            return position_qty
        return max(1, math.floor(position_qty * reduce_pct / 100))


def direction_of(strategy: str) -> str:
    return Strategy(strategy).direction
