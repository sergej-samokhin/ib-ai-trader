---
contract_version: "1.0"
signal_id: "spy-hedge-2026-10-19-001"
version: 1
kind: trade
producer: "macro-agent"
created_at: "2026-10-19T14:00:00Z"
valid_until: "2026-10-19T19:30:00Z"
confidence: 0.55
underlying: SPY
strategy: long_put
contract:
  dte_min: 30
  dte_max: 60
  long_leg_delta: 0.30
order:
  type: limit
  price: mid
  max_reprice_steps: 2
sizing:
  max_premium_usd: 250
  max_contracts: 1
exit:
  take_profit_pct: 80
  stop_loss_pct: 40
  close_before_expiry_days: 10
---

## Rationale
Breadth deteriorating while index sits near highs; buying a small put as a hedge.
