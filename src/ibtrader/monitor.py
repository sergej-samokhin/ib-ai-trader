"""Position monitor: take-profit, stop-loss and time-based exits."""

from __future__ import annotations

import json
from datetime import date

from ibtrader.clock import dte
from ibtrader.domain import OptionQuote, close_prices, legs_from_json


def exit_reason(
    position, quotes: list[OptionQuote], today: date
) -> tuple[str | None, float | None]:
    """Returns (reason, pnl_pct). Reason is take_profit | stop_loss | time_exit | None."""
    rules = json.loads(position["exit_rules"])
    legs = legs_from_json(position["legs"])
    if dte(min(leg.expiry for leg in legs), today) <= rules["close_before_expiry_days"]:
        return "time_exit", None
    mid, _ = close_prices(legs, quotes)
    entry = position["entry_price"]
    if mid is None or entry <= 0:
        return None, None
    pnl_pct = (mid - entry) / entry * 100
    if pnl_pct >= rules["take_profit_pct"]:
        return "take_profit", pnl_pct
    if pnl_pct <= -rules["stop_loss_pct"]:
        return "stop_loss", pnl_pct
    return None, pnl_pct
