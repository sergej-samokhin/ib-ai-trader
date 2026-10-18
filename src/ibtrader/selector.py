"""Contract Selector: resolve expiry (DTE window), strikes (by delta) and right for a strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ibtrader.clock import dte
from ibtrader.contract.models import ContractSpec, Strategy
from ibtrader.domain import Leg, OptionQuote, net_prices
from ibtrader.marketdata import MarketData


class SelectionError(Exception):
    pass


@dataclass(frozen=True)
class Selection:
    legs: list[Leg]
    quotes: list[OptionQuote]
    expiry: date
    mid: float  # net debit at mid, per share
    natural: float  # worst-case net debit (pay ask, receive bid), per share


def choose_expiry(expiries: list[date], spec: ContractSpec, today: date) -> date:
    in_window = [e for e in expiries if spec.dte_min <= dte(e, today) <= spec.dte_max]
    if not in_window:
        raise SelectionError(f"no expiry with DTE in [{spec.dte_min}, {spec.dte_max}]")
    target = (spec.dte_min + spec.dte_max) / 2
    return min(sorted(in_window), key=lambda e: abs(dte(e, today) - target))


def _closest(quotes: list[OptionQuote], target: float, tolerance: float) -> OptionQuote:
    usable = [q for q in quotes if q.delta is not None and q.mid is not None]
    if not usable:
        raise SelectionError("no quotes with delta and two-sided market")
    best = min(usable, key=lambda q: (abs(abs(q.delta) - target), q.strike))  # type: ignore[arg-type]
    if abs(abs(best.delta) - target) > tolerance:  # type: ignore[arg-type]
        raise SelectionError(
            f"closest delta {abs(best.delta):.2f} is more than {tolerance:.2f} from {target:.2f}"  # type: ignore[arg-type]
        )
    return best


def select(
    strategy: Strategy,
    spec: ContractSpec,
    md: MarketData,
    symbol: str,
    today: date,
    delta_tolerance: float = 0.10,
) -> Selection:
    expiry = choose_expiry(md.expirations(symbol), spec, today)
    right = strategy.right
    chain = [q for q in md.option_quotes(symbol, expiry, right) if q.right == right]
    long_q = _closest(chain, spec.long_leg_delta, delta_tolerance)
    legs = [Leg(symbol, expiry, long_q.strike, right, "BUY", 1, long_q.con_id)]
    quotes = [long_q]

    if strategy.is_spread:
        assert spec.short_leg_delta is not None
        further_otm = [
            q
            for q in chain
            if (q.strike > long_q.strike if right == "C" else q.strike < long_q.strike)
        ]
        short_q = _closest(further_otm, spec.short_leg_delta, delta_tolerance)
        legs.append(Leg(symbol, expiry, short_q.strike, right, "SELL", 1, short_q.con_id))
        quotes.append(short_q)

    mid, natural = net_prices(legs, quotes)
    if mid is None or natural is None or mid <= 0:
        raise SelectionError("position does not price as a debit")
    return Selection(legs=legs, quotes=quotes, expiry=expiry, mid=mid, natural=max(natural, mid))
