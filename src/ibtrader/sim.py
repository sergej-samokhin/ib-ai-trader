"""Offline simulation: synthetic market data and a simulated broker.

Used for tests, `ibtrader run --demo`, and shadow execution of `paper_only` producers in live mode.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from ibtrader.clock import ny_date, utcnow
from ibtrader.domain import Bar, Leg, OptionQuote, Right, close_prices, net_prices
from ibtrader.orders import BrokerOrderStatus


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes(
    spot: float, strike: float, years: float, vol: float, rate: float, right: Right
) -> tuple[float, float]:
    """(price, delta) of a European option."""
    years = max(years, 1 / 365)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if right == "C":
        price = spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1
    return max(price, 0.0), delta


def _con_id(symbol: str, expiry: date, strike: float, right: str) -> int:
    digest = hashlib.sha256(f"{symbol}{expiry}{strike}{right}".encode()).hexdigest()
    return int(digest[:8], 16)


class FakeMarketData:
    def __init__(
        self,
        spots: dict[str, float] | None = None,
        vol: float = 0.35,
        rate: float = 0.04,
        open_interest: int = 1500,
        today: Callable[[], date] | None = None,
        seed: int = 7,
    ):
        self.spots = dict(spots or {})
        self.vol = vol
        self.rate = rate
        self.open_interest = open_interest
        self.today = today or (lambda: ny_date(utcnow()))
        self.bars: dict[str, list[Bar]] = {}
        self.seed = seed

    def spot(self, symbol: str) -> float:
        return self.spots.setdefault(symbol, 100.0)

    def set_spot(self, symbol: str, price: float) -> None:
        self.spots[symbol] = price

    # -- MarketData protocol ------------------------------------------------------
    def daily_bars(self, symbol: str, count: int) -> list[Bar]:
        if symbol in self.bars:
            return self.bars[symbol][-count:]
        rng = random.Random(f"{self.seed}-{symbol}")
        closes = [self.spot(symbol)]
        for _ in range(count - 1):
            closes.append(closes[-1] / (1 + rng.gauss(0.0008, 0.015)))
        closes.reverse()
        start = self.today() - timedelta(days=count)
        return [
            Bar(start + timedelta(days=i), c * 0.995, c * 1.01, c * 0.99, c, 1_000_000 + i * 1000)
            for i, c in enumerate(closes)
        ]

    def expirations(self, symbol: str) -> list[date]:
        today = self.today()
        first_friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        return [first_friday + timedelta(weeks=w) for w in range(16)]

    def _strike_step(self, spot: float) -> float:
        return 1.0 if spot < 50 else 2.5 if spot < 200 else 5.0

    def _quote(self, symbol: str, expiry: date, strike: float, right: Right) -> OptionQuote:
        spot = self.spot(symbol)
        years = max((expiry - self.today()).days, 0) / 365
        theo, delta = black_scholes(spot, strike, years, self.vol, self.rate, right)
        half = max(0.02, theo * 0.03)
        return OptionQuote(
            symbol=symbol,
            expiry=expiry,
            strike=strike,
            right=right,
            bid=round(max(0.0, theo - half), 2),
            ask=round(theo + half, 2),
            delta=round(delta, 4),
            open_interest=self.open_interest,
            con_id=_con_id(symbol, expiry, strike, right),
        )

    def option_quotes(self, symbol: str, expiry: date, right: Right) -> list[OptionQuote]:
        spot = self.spot(symbol)
        step = self._strike_step(spot)
        center = round(spot / step) * step
        strikes = [center + step * i for i in range(-15, 16) if center + step * i > 0]
        return [self._quote(symbol, expiry, k, right) for k in strikes]

    def quote_legs(self, legs: list[Leg]) -> list[OptionQuote]:
        return [self._quote(leg.symbol, leg.expiry, leg.strike, leg.right) for leg in legs]


@dataclass
class _SimOrder:
    legs: list[Leg]
    action: str
    quantity: int
    limit_price: float
    order_ref: str
    state: str = "working"
    filled: int = 0
    avg_price: float | None = None
    history: list[float] = field(default_factory=list)


class SimBroker:
    """Fills when the limit crosses the current mid (fill_mode='mid'), or never/immediately."""

    def __init__(self, md, fill_mode: str = "mid"):
        self.md = md
        self.fill_mode = fill_mode  # mid | always | never | partial
        self.orders: dict[str, _SimOrder] = {}
        self._next = 1

    def submit(self, legs, action, quantity, limit_price, order_ref) -> str:
        order_id = f"sim-{self._next}"
        self._next += 1
        self.orders[order_id] = _SimOrder(list(legs), action, quantity, limit_price, order_ref)
        self.orders[order_id].history.append(limit_price)
        return order_id

    def modify(self, order_id: str, limit_price: float) -> None:
        order = self.orders[order_id]
        order.limit_price = limit_price
        order.history.append(limit_price)

    def cancel(self, order_id: str) -> None:
        order = self.orders[order_id]
        if order.state == "working":
            order.state = "cancelled"

    def _crosses(self, order: _SimOrder) -> bool:
        if self.fill_mode == "always":
            return True
        if self.fill_mode == "never":
            return False
        quotes = self.md.quote_legs(order.legs)
        if order.action == "BUY":
            mid, _ = net_prices(order.legs, quotes)
            return mid is not None and order.limit_price >= mid - 1e-9
        mid, _ = close_prices(order.legs, quotes)
        return mid is not None and order.limit_price <= mid + 1e-9

    def poll(self, order_id: str) -> BrokerOrderStatus:
        order = self.orders[order_id]
        if order.state == "working" and self._crosses(order):
            if self.fill_mode == "partial" and order.quantity > 1 and order.filled == 0:
                order.filled = order.quantity // 2
            else:
                order.filled = order.quantity
                order.state = "filled"
            order.avg_price = order.limit_price
        state = order.state if order.state in ("filled", "cancelled", "rejected") else "working"
        return BrokerOrderStatus(state, order.filled, order.avg_price)  # type: ignore[arg-type]

    def _by_ref(self, order_ref: str) -> str | None:
        for order_id, order in self.orders.items():
            if order.order_ref == order_ref:
                return order_id
        return None

    def find_by_ref(self, order_ref: str) -> str | None:
        """Working orders only, like IBKR's openTrades()."""
        order_id = self._by_ref(order_ref)
        if order_id is None or self.poll(order_id).state != "working":
            return None
        return order_id

    def lookup_final(self, order_ref: str) -> BrokerOrderStatus | None:
        order_id = self._by_ref(order_ref)
        if order_id is None:
            return None
        status = self.poll(order_id)
        return None if status.state == "working" else status

    def reset(self) -> None:
        pass  # simulated orders keep their state in memory

    def positions(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for order in self.orders.values():
            if not order.filled:
                continue
            sign = 1 if order.action == "BUY" else -1
            for leg in order.legs:
                leg_sign = sign * (1 if leg.action == "BUY" else -1)
                key = leg.con_id or 0
                out[key] = out.get(key, 0) + leg_sign * order.filled * leg.ratio
        return {k: v for k, v in out.items() if v}
