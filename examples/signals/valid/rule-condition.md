---
contract_version: "1.0"
signal_id: "aapl-sma-cross"
version: 1
kind: rule
producer: "momentum-agent"
created_at: "2026-10-19T12:00:00Z"
valid_until: "2026-11-30T21:00:00Z"
confidence: 0.58
underlying: AAPL
strategy: bull_call_spread
contract:
  dte_min: 21
  dte_max: 45
  long_leg_delta: 0.45
  short_leg_delta: 0.25
order:
  type: limit
  price: mid
  max_reprice_steps: 3
sizing:
  max_premium_usd: 300
  max_contracts: 2
exit:
  take_profit_pct: 50
  stop_loss_pct: 50
  close_before_expiry_days: 5
trigger:
  type: condition
  condition: "close > sma_20 and rsi_14 < 70"
  check_at: "15:30 America/New_York"
max_entries_per_day: 1
---

## Rationale
Trend-following entry once price reclaims the 20-day average without being overbought.
