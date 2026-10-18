"""Position monitor: take-profit, stop-loss, expiry and holding-period exits."""

from __future__ import annotations

import json
from datetime import date

from ibtrader.clock import dte, ny_date, trading_days_between
from ibtrader.domain import OptionQuote, close_prices, legs_from_json
from ibtrader.store import parse_iso


def exit_reason(
    position, quotes: list[OptionQuote], today: date
) -> tuple[str | None, float | None]:
    """Returns (reason, pnl_pct).

    Reason is take_profit | stop_loss | time_exit | holding_exit | None. `holding_exit` fires once
    the position has been held `max_holding_days` sessions (the entry day itself does not count).
    Precedence: time_exit, then holding_exit, then take_profit / stop_loss.
    """
    rules = json.loads(position["exit_rules"])
    legs = legs_from_json(position["legs"])
    if dte(min(leg.expiry for leg in legs), today) <= rules["close_before_expiry_days"]:
        return "time_exit", None
    max_days = rules.get("max_holding_days")
    opened = ny_date(parse_iso(position["opened_at"]))
    if max_days and trading_days_between(opened, today) >= max_days:
        return "holding_exit", None
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
