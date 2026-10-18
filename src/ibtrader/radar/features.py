"""Attention features of strategy section 3.3 on top of the 24 h analytics rows.

- ``attention_z``: robust z-score of today's ``mentions_24h`` against the ticker's own last
  30 tracked days, (x - median) / max(1.4826 * MAD, 1). The floor of 1 keeps sparse tickers
  (median and MAD of 0) from producing infinite scores; a tracked day without the ticker
  counts as 0 mentions. None until ``MIN_BASELINE_DAYS`` days are tracked.
- ``attention_accel``: ``mentions_24h`` / mentions in the 24 h before, None without coverage
  of that window or with 0 mentions in it.
- ``history_days``: tracked days in the baseline; the strategy's data-quality filter wants 30.
"""

from __future__ import annotations

import statistics

BASELINE_DAYS = 30
MIN_BASELINE_DAYS = 7
MAD_SCALE = 1.4826


def robust_z(value: float, baseline: list[float]) -> float | None:
    if len(baseline) < MIN_BASELINE_DAYS:
        return None
    median = statistics.median(baseline)
    mad = statistics.median(abs(x - median) for x in baseline)
    return round((value - median) / max(MAD_SCALE * mad, 1.0), 2)


def enrich(
    rows: list[dict],
    baseline: dict[str, dict[str, int]],
    baseline_days: list[str],
    previous_24h: dict[str, int] | None,
) -> list[dict]:
    """Add ``attention_z``, ``attention_accel`` and ``history_days`` to each row in place.

    ``previous_24h`` is None when the collector did not cover the preceding 24 hours.
    """
    for row in rows:
        own = baseline.get(row["ticker"], {})
        series = [float(own.get(day, 0)) for day in baseline_days]
        row["attention_z"] = robust_z(float(row["mentions_24h"]), series)
        previous = previous_24h.get(row["ticker"], 0) if previous_24h is not None else 0
        row["attention_accel"] = round(row["mentions_24h"] / previous, 2) if previous else None
        row["history_days"] = len(baseline_days)
    return rows
