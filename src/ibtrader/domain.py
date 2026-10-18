"""Broker-neutral domain types shared by selector, risk engine and order manager."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

Right = Literal["C", "P"]
Action = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: date
    strike: float
    right: Right
    bid: float | None
    ask: float | None
    delta: float | None
    open_interest: int | None
    con_id: int | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid < 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * 100


@dataclass(frozen=True)
class Leg:
    symbol: str
    expiry: date
    strike: float
    right: Right
    action: Action  # action of this leg when the position is OPENED
    ratio: int = 1
    con_id: int | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expiry"] = self.expiry.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Leg:
        return cls(**{**data, "expiry": date.fromisoformat(data["expiry"])})


def legs_to_json(legs: list[Leg]) -> str:
    return json.dumps([leg.to_dict() for leg in legs])


def legs_from_json(text: str) -> list[Leg]:
    return [Leg.from_dict(item) for item in json.loads(text)]


def net_prices(legs: list[Leg], quotes: list[OptionQuote]) -> tuple[float | None, float | None]:
    """(mid, natural) net debit to OPEN the position; natural = pay ask / receive bid.

    For closing, the natural credit is computed by `close_prices`.
    """
    mid = 0.0
    natural = 0.0
    for leg, quote in zip(legs, quotes, strict=True):
        if quote.mid is None or quote.ask is None or quote.bid is None:
            return None, None
        sign = 1 if leg.action == "BUY" else -1
        mid += sign * quote.mid * leg.ratio
        natural += sign * (quote.ask if sign > 0 else quote.bid) * leg.ratio
    return mid, natural


def close_prices(legs: list[Leg], quotes: list[OptionQuote]) -> tuple[float | None, float | None]:
    """(mid, natural) net credit received to CLOSE: sell long legs at bid, buy short at ask."""
    mid = 0.0
    natural = 0.0
    for leg, quote in zip(legs, quotes, strict=True):
        if quote.mid is None or quote.ask is None or quote.bid is None:
            return None, None
        sign = 1 if leg.action == "BUY" else -1
        mid += sign * quote.mid * leg.ratio
        natural += sign * (quote.bid if sign > 0 else quote.ask) * leg.ratio
    return mid, natural
