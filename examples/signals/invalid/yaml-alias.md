---
# INVALID: YAML anchors/aliases are rejected (billion-laughs protection)
contract_version: "1.0"
signal_id: &id "alias-test-001"
version: 1
kind: exit
producer: "momentum-agent"
created_at: "2026-10-19T17:00:00Z"
valid_until: "2026-10-19T19:55:00Z"
confidence: 0.7
target_signal_id: *id
action: close
---
