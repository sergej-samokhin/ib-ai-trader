"""Signal Evaluator for standing `rule` signals."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ibtrader import expr, indicators
from ibtrader.clock import ny_date, parse_check_at, to_ny
from ibtrader.contract.models import RuleSignal
from ibtrader.marketdata import MarketData
from ibtrader.store import parse_iso


class RuleEvaluator:
    def __init__(self, md: MarketData, interval_seconds: float):
        self.md = md
        self.interval = interval_seconds

    def is_due(self, row, rule: RuleSignal, now: datetime) -> bool:
        today = ny_date(now).isoformat()
        if row["entries_date"] == today and row["entries_today"] >= rule.max_entries_per_day:
            return False
        if rule.trigger.check_at:
            at = parse_check_at(rule.trigger.check_at)
            return to_ny(now).time() >= at and row["last_eval_date"] != today
        last = row["last_eval_at"]
        return last is None or (now - parse_iso(last)).total_seconds() >= self.interval

    def evaluate(self, rule: RuleSignal) -> tuple[bool, dict[str, Any]]:
        """Returns (fired, indicator values used). Errors never fire a rule."""
        if rule.trigger.type == "schedule":
            return True, {}
        assert rule.trigger.condition is not None
        tree = expr.parse(rule.trigger.condition)
        names = expr.indicators_used(tree)
        bars = self.md.daily_bars(rule.underlying, indicators.bars_needed(names))
        values = indicators.compute(names, bars)
        try:
            fired = bool(expr.evaluate(tree, values))  # type: ignore[arg-type]
        except expr.EvaluationError as exc:
            return False, {**values, "error": str(exc)}
        return fired, values
