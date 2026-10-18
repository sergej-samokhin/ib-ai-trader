# Changelog

## 0.1.0 – 2026-10-18

First version built from the build spec (draft v0.2).

- Signal Contract 1.0 (`trade`, `rule`, `exit`) as strict pydantic models; JSON Schema export;
  `SIGNAL_CONTRACT.md`; valid, invalid and adversarial example signals.
- Signal Gateway: producer authentication, per-producer hourly rate limit, raw payloads stored
  verbatim, deduplication on `signal_id` + `version`, TTL, rule versioning, immutable trades
  and exits, conflict policy (newer opposite-direction signal rejected).
- Transports: file drop (default) and authenticated HTTP endpoint (ADR 0001).
- Whitelisted condition language with indicators `close`, `open`, `high`, `low`, `volume`,
  `prev_close`, `sma_N`, `ema_N`, `rsi_N`, `avg_volume_N`.
- Contract selector by DTE window and delta; risk engine with all spec section 6 guardrails;
  manual approval flow (CLI); shadow execution for `paper_only` producers in live mode.
- Order manager: limit at mid, bounded reprice ladder, partial fills, combo orders for spreads,
  exits that keep working at the natural price.
- Position monitor (take profit, stop loss, close before expiry), kill switch, daily summary,
  per-producer scorecard, restart recovery by `orderRef`, position reconciliation.
- IBKR adapter on `ib_async` (not yet validated against a paper account), simulation backend,
  `ibtrader` CLI, Docker / compose, CI.
