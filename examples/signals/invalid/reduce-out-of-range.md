---
contract_version: "1.0"
signal_id: "aapl-sma-cross-reduce-001"
version: 1
kind: exit
producer: "momentum-agent"
created_at: "2026-10-20T15:00:00Z"
valid_until: "2026-10-20T19:55:00Z"
confidence: 0.6
target_signal_id: "aapl-sma-cross"
action: reduce
reduce_pct: 150   # INVALID: reduce_pct must be 1..99 for reduce
---

## Rationale
Take half off ahead of the CPI release.
