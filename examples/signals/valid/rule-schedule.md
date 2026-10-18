---
contract_version: "1.0"
signal_id: "qqq-weekly-put-spread"
version: 2
kind: rule
producer: "macro-agent"
created_at: "2026-10-19T12:00:00Z"
valid_until: "2026-12-31T21:00:00Z"
confidence: 0.5
underlying: QQQ
strategy: bear_put_spread
contract:
  dte_min: 14
  dte_max: 30
  long_leg_delta: 0.35
  short_leg_delta: 0.15
order:
  type: limit
  price: mid
  max_reprice_steps: 3
sizing:
  max_premium_usd: 200
  max_contracts: 1
exit:
  take_profit_pct: 100
  stop_loss_pct: 50
  close_before_expiry_days: 3
trigger:
  type: schedule
  check_at: "10:30 America/New_York"
max_entries_per_day: 1
---

## Rationale
Version 2 replaces version 1 (narrower deltas). Systematic small downside hedge.
