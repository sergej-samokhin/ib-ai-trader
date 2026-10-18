"""Indicator calculations for the condition language. Pure functions over daily bars."""

from __future__ import annotations

from collections.abc import Iterable

from ibtrader import expr
from ibtrader.domain import Bar


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * k + current * (1 - k)
    return current


def rsi(values: list[float], period: int) -> float | None:
    """Wilder's RSI."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(values, values[1:], strict=False):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def bars_needed(names: Iterable[str]) -> int:
    longest = max((expr.indicator_period(n) for n in names), default=1)
    return max(longest * 3, longest + 2, 30)


def compute(names: Iterable[str], bars: list[Bar]) -> dict[str, float | None]:
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    out: dict[str, float | None] = {}
    for name in names:
        if not bars:
            out[name] = None
            continue
        last = bars[-1]
        if name == "close":
            out[name] = last.close
        elif name == "open":
            out[name] = last.open
        elif name == "high":
            out[name] = last.high
        elif name == "low":
            out[name] = last.low
        elif name == "volume":
            out[name] = last.volume
        elif name == "prev_close":
            out[name] = bars[-2].close if len(bars) >= 2 else None
        else:
            kind, _, period_text = name.rpartition("_")
            period = int(period_text)
            if kind == "sma":
                out[name] = sma(closes, period)
            elif kind == "ema":
                out[name] = ema(closes, period)
            elif kind == "rsi":
                out[name] = rsi(closes, period)
            elif kind == "avg_volume":
                out[name] = sma(volumes, period)
            else:  # pragma: no cover - names are validated by expr
                out[name] = None
    return out
