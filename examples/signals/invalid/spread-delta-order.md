---
contract_version: "1.0"
signal_id: "nvda-momo-2026-10-19-001"
version: 1
kind: trade
producer: "momentum-agent"
created_at: "2026-10-19T13:40:00Z"
valid_until: "2026-10-19T19:55:00Z"
confidence: 0.62
underlying: NVDA
strategy: bull_call_spread
contract:
  dte_min: 21
  dte_max: 45
  long_leg_delta: 0.40
  short_leg_delta: 0.50   # INVALID: short leg must be further OTM than the long leg
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
---

## Rationale
