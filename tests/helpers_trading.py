"""Shared builders for the risk, selector, order and lifecycle tests (no conftest on purpose)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ibtrader.app import Trader
from ibtrader.clock import ny_date
from ibtrader.config import RiskLimits, Settings
from ibtrader.contract.models import ContractSpec
from ibtrader.domain import Bar, Leg, OptionQuote, net_prices
from ibtrader.killswitch import KillSwitch
from ibtrader.notify import LogNotifier
from ibtrader.producers import Producer, ProducerRegistry
from ibtrader.risk import EntryRequest, PortfolioSnapshot, RiskEngine
from ibtrader.selector import Selection
from ibtrader.sim import FakeMarketData, SimBroker
from ibtrader.store import Store

# Monday 2026-10-19, 10:30 New York (EDT): inside the default entry window.
NOW = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
EXPIRY = date(2026, 11, 20)


# ================================================================================= risk builders
def quote(
    strike: float = 100.0,
    right: str = "C",
    *,
    bid: float | None = 1.00,
    ask: float | None = 1.10,
    delta: float | None = 0.40,
    open_interest: int | None = 1000,
    symbol: str = "NVDA",
    expiry: date = EXPIRY,
    con_id: int | None = None,
) -> OptionQuote:
    return OptionQuote(
        symbol,
        expiry,
        strike,
        right,
        bid,
        ask,
        delta,
        open_interest,
        con_id or int(strike * 10),  # type: ignore[arg-type]
    )


def selection(*quotes: OptionQuote) -> Selection:
    """Selection from hand-built quotes: first quote is bought, a second one is sold."""
    quotes = quotes or (quote(),)
    legs = [
        Leg(q.symbol, q.expiry, q.strike, q.right, "BUY" if i == 0 else "SELL", 1, q.con_id)  # type: ignore[arg-type]
        for i, q in enumerate(quotes)
    ]
    mid, natural = net_prices(legs, list(quotes))
    assert mid is not None and natural is not None
    return Selection(legs, list(quotes), quotes[0].expiry, mid, max(natural, mid))


def producer(
    producer_id: str = "momentum-agent", status: str = "auto", budget_usd: float = 1000.0
) -> Producer:
    return Producer(id=producer_id, status=status, budget_usd=budget_usd)  # type: ignore[arg-type]


def entry_request(**overrides: Any) -> EntryRequest:
    """An EntryRequest that passes every rule; override single fields per test.

    Defaults: one call 1.00/1.10 (mid 1.05, natural 1.10 -> 110 USD per contract), signal asks
    300 USD / 2 contracts, so the baseline quantity is 2 and the premium stays below the manual
    approval threshold.
    """
    fields: dict[str, Any] = {
        "producer": producer(),
        "producer_paused": False,
        "underlying": "NVDA",
        "strategy": "long_call",
        "order_type": "limit",
        "max_premium_usd": 300.0,
        "max_contracts": 2,
        "confidence": 0.6,
        "selection": selection(),
        "now": NOW,
        "shadow": False,
    }
    fields.update(overrides)
    return EntryRequest(**fields)


def portfolio(**overrides: Any) -> PortfolioSnapshot:
    return PortfolioSnapshot(**overrides)


def risk_engine(tmp_path: Path, **limits: Any) -> RiskEngine:
    return RiskEngine(RiskLimits(**limits), KillSwitch(tmp_path / "KILL"))


# ============================================================================= selector builders
def spec(**overrides: Any) -> ContractSpec:
    fields: dict[str, Any] = {
        "dte_min": 21,
        "dte_max": 45,
        "long_leg_delta": 0.40,
        "short_leg_delta": 0.20,
    }
    fields.update(overrides)
    return ContractSpec(**fields)


class StubMarketData:
    """Hand-made chain for selector edge cases (one expiry, quotes as given)."""

    def __init__(self, quotes: list[OptionQuote], expiries: list[date] | None = None):
        self.quotes = quotes
        self.expiries = expiries or [EXPIRY]

    def expirations(self, symbol: str) -> list[date]:
        return self.expiries

    def option_quotes(self, symbol: str, expiry: date, right: str) -> list[OptionQuote]:
        return list(self.quotes)

    def quote_legs(self, legs: list[Leg]) -> list[OptionQuote]:
        raise NotImplementedError

    def daily_bars(self, symbol: str, count: int) -> list[Bar]:
        raise NotImplementedError


class SpyMarketData(FakeMarketData):
    """FakeMarketData that records which rights were requested."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.requested_rights: list[str] = []

    def option_quotes(self, symbol: str, expiry: date, right: str) -> list[OptionQuote]:  # type: ignore[override]
        self.requested_rights.append(right)
        return super().option_quotes(symbol, expiry, right)  # type: ignore[arg-type]


# ================================================================================ signal builders
CREATED = "2026-10-19T13:00:00Z"
VALID_UNTIL = "2026-10-19T19:55:00Z"


def _render(front: dict[str, Any]) -> str:
    text = yaml.safe_dump(front, sort_keys=False)
    return f"---\n{text}---\n\n## Rationale\nTest signal.\n"


def trade_signal(
    signal_id: str = "nvda-momo-001",
    *,
    producer_id: str = "momentum-agent",
    underlying: str = "NVDA",
    strategy: str = "bull_call_spread",
    long_delta: float = 0.40,
    short_delta: float | None = 0.20,
    dte_min: int = 21,
    dte_max: int = 45,
    max_premium_usd: float = 300,
    max_contracts: int = 2,
    confidence: float = 0.62,
    take_profit_pct: float = 50,
    stop_loss_pct: float = 50,
    close_before_expiry_days: int = 5,
    max_holding_days: int | None = None,
    max_reprice_steps: int = 3,
    created_at: str = CREATED,
    valid_until: str = VALID_UNTIL,
) -> str:
    contract: dict[str, Any] = {
        "dte_min": dte_min,
        "dte_max": dte_max,
        "long_leg_delta": long_delta,
    }
    if strategy in ("bull_call_spread", "bear_put_spread"):
        contract["short_leg_delta"] = short_delta
    exit_rules: dict[str, Any] = {
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "close_before_expiry_days": close_before_expiry_days,
    }
    if max_holding_days is not None:
        exit_rules["max_holding_days"] = max_holding_days
    return _render(
        {
            "contract_version": "1.0",
            "signal_id": signal_id,
            "version": 1,
            "kind": "trade",
            "producer": producer_id,
            "created_at": created_at,
            "valid_until": valid_until,
            "confidence": confidence,
            "underlying": underlying,
            "strategy": strategy,
            "contract": contract,
            "order": {"type": "limit", "price": "mid", "max_reprice_steps": max_reprice_steps},
            "sizing": {"max_premium_usd": max_premium_usd, "max_contracts": max_contracts},
            "exit": exit_rules,
        }
    )


def rule_signal(
    signal_id: str = "aapl-sma-cross",
    *,
    producer_id: str = "momentum-agent",
    underlying: str = "AAPL",
    condition: str | None = "close > sma_20 and rsi_14 < 70",
    check_at: str | None = "15:30 America/New_York",
    max_entries_per_day: int = 1,
    version: int = 1,
    long_delta: float = 0.45,
    short_delta: float = 0.25,
    strategy: str = "bull_call_spread",
    created_at: str = CREATED,
    valid_until: str = "2026-11-30T21:00:00Z",
) -> str:
    trigger: dict[str, Any] = {"type": "condition" if condition else "schedule"}
    if condition:
        trigger["condition"] = condition
    if check_at:
        trigger["check_at"] = check_at
    front = yaml.safe_load(
        trade_signal(
            signal_id,
            producer_id=producer_id,
            underlying=underlying,
            strategy=strategy,
            long_delta=long_delta,
            short_delta=short_delta,
            created_at=created_at,
            valid_until=valid_until,
        ).split("---\n")[1]
    )
    front["kind"] = "rule"
    front["version"] = version
    front["trigger"] = trigger
    front["max_entries_per_day"] = max_entries_per_day
    return _render(front)


def exit_signal(
    target: str = "nvda-momo-001",
    *,
    signal_id: str | None = None,
    producer_id: str = "momentum-agent",
    action: str = "close",
    reduce_pct: int | None = None,
    created_at: str = CREATED,
    valid_until: str = VALID_UNTIL,
) -> str:
    front: dict[str, Any] = {
        "contract_version": "1.0",
        "signal_id": signal_id or f"{target}-exit",
        "version": 1,
        "kind": "exit",
        "producer": producer_id,
        "created_at": created_at,
        "valid_until": valid_until,
        "confidence": 0.7,
        "target_signal_id": target,
        "action": action,
    }
    if reduce_pct is not None:
        front["reduce_pct"] = reduce_pct
    return _render(front)


def trending_bars(up: bool, count: int = 60, base: float = 100.0) -> list[Bar]:
    """Deterministic daily bars. up: +1 / -0.5 pattern (close > sma_20, RSI about 66)."""
    closes = [base]
    for i in range(count - 1):
        step = 1.0 if i % 2 == 0 else -0.5
        closes.append(closes[-1] + (step if up else -step))
    start = date(2026, 8, 1)
    return [
        Bar(start + timedelta(days=i), c, c + 1, c - 1, c, 1_000_000.0)
        for i, c in enumerate(closes)
    ]


# ====================================================================================== harness
class FailingBroker(SimBroker):
    """SimBroker whose submit raises (gateway disconnect / rejected at the door)."""

    def submit(self, legs, action, quantity, limit_price, order_ref) -> str:  # type: ignore[override]
        raise ConnectionError("gateway disconnected")


def default_producers() -> list[Producer]:
    return [
        Producer(id="momentum-agent", status="auto", budget_usd=1000),  # type: ignore[arg-type]
        Producer(id="macro-agent", status="auto", budget_usd=1000),  # type: ignore[arg-type]
        Producer(id="manual-agent", status="manual", budget_usd=1000),  # type: ignore[arg-type]
        Producer(id="paper-agent", status="paper_only", budget_usd=1000),  # type: ignore[arg-type]
    ]


@dataclass
class Harness:
    trader: Trader
    md: FakeMarketData
    broker: SimBroker
    shadow_broker: SimBroker
    notifier: LogNotifier
    settings: Settings
    clock: list[datetime] = field(default_factory=lambda: [NOW])

    @property
    def store(self) -> Store:
        return self.trader.store

    @property
    def now(self) -> datetime:
        return self.clock[0]

    def at(self, moment: datetime) -> Harness:
        self.clock[0] = moment
        return self

    def submit(self, raw: str, producer_id: str = "momentum-agent", now: datetime | None = None):
        return self.trader.gateway.submit(raw, producer_id, "test", now or self.now)

    def run(self, ticks: int = 1, seconds: float = 25) -> datetime:
        """Tick `ticks` times, advancing the explicit clock `seconds` after each tick."""
        for _ in range(ticks):
            self.trader.tick(self.now)
            self.clock[0] = self.now + timedelta(seconds=seconds)
        return self.now

    def run_until(
        self, predicate: Callable[[], bool], max_ticks: int = 60, seconds: float = 25
    ) -> None:
        for _ in range(max_ticks):
            if predicate():
                return
            self.run(1, seconds)
        assert predicate(), "condition not reached within the tick budget"

    # -- store shortcuts ------------------------------------------------------------------
    def intents(self, **where: Any) -> list[Any]:
        rows = self.store.query("SELECT * FROM intents ORDER BY created_at, rowid")
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def intent(self, **where: Any):
        rows = self.intents(**where)
        assert len(rows) == 1, f"expected exactly one intent for {where}, got {len(rows)}"
        return rows[0]

    def positions(self, **where: Any) -> list[Any]:
        rows = self.store.query("SELECT * FROM positions ORDER BY opened_at, rowid")
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def signal_status(self, producer_id: str, signal_id: str, version: int = 1) -> str:
        return self.store.get_signal(producer_id, signal_id, version)["status"]

    def audit_events(self, event: str) -> list[Any]:
        return self.store.query("SELECT * FROM audit_log WHERE event = ? ORDER BY id", (event,))

    def notifications(self, subject_part: str) -> list[tuple[str, str, str]]:
        return [n for n in self.notifier.sent if subject_part in n[0]]

    def fill_entry(self, signal_id: str = "nvda-momo-001", producer_id: str = "momentum-agent"):
        """Tick until the entry for this signal has filled and a position exists."""
        self.run_until(
            lambda: any(
                p["signal_id"] == signal_id and p["producer"] == producer_id
                for p in self.positions()
            )
        )
        return next(
            p
            for p in self.positions()
            if p["signal_id"] == signal_id and p["producer"] == producer_id
        )


def make_harness(
    tmp_path: Path,
    *,
    mode: str = "paper",
    producers: list[Producer] | None = None,
    spots: dict[str, float] | None = None,
    fill_mode: str = "mid",
    shadow_fill_mode: str = "mid",
    **risk: Any,
) -> Harness:
    clock = [NOW]
    settings = Settings(mode=mode, data_dir=tmp_path, risk=RiskLimits(**risk))  # type: ignore[arg-type]
    md = FakeMarketData(
        spots={"NVDA": 180.0, "AAPL": 100.0, "QQQ": 100.0, "AMD": 100.0, **(spots or {})},
        today=lambda: ny_date(clock[0]),
    )
    broker = SimBroker(md, fill_mode)
    shadow = SimBroker(md, shadow_fill_mode)
    notifier = LogNotifier()
    trader = Trader(
        settings,
        ProducerRegistry(producers or default_producers()),
        Store(":memory:"),
        md,
        broker,
        notifier,
        shadow_broker=shadow,
    )
    return Harness(trader, md, broker, shadow, notifier, settings, clock)
