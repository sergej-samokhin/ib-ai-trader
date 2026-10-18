"""Market data interface. Implementations: `ibkr.IBKRMarketData` and `sim.FakeMarketData`."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ibtrader.domain import Bar, Leg, OptionQuote, Right


class MarketData(Protocol):
    def daily_bars(self, symbol: str, count: int) -> list[Bar]:
        """Most recent `count` daily bars, oldest first; the last bar may be today's partial."""

    def expirations(self, symbol: str) -> list[date]:
        """Listed option expiries for the underlying."""

    def option_quotes(self, symbol: str, expiry: date, right: Right) -> list[OptionQuote]:
        """Quotes incl. delta and open interest for strikes around the money."""

    def quote_legs(self, legs: list[Leg]) -> list[OptionQuote]:
        """Fresh quotes for specific legs, same order as `legs`."""
