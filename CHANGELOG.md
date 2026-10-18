# Changelog

## Unreleased

- New risk limit `MAX_MONTHLY_LOSS_USD` (rule `monthly_loss_limit`, placeholder default 3000):
  rejects entries once the calendar month's net realised P&L reaches minus the limit, and sizes
  entries so that this P&L minus all premium at risk never falls below it.
- Session calendar now uses `pandas_market_calendars` (NYSE) instead of a hand-written
  2026-2027 holiday table; the 2026-2027 dates are pinned in the tests.
- `docs/RELATED_PROJECTS.md`: survey of related open-source repositories.
- Recovery books orders that finished while the trader was away (IBKR completed orders and
  executions of the current day) and runs again after every reconnect.
- `ibtrader intent resolve` and `ibtrader position close` replace the SQL workaround for
  correcting the store.
- One alert per IBKR outage (`IBKR connection lost`, reminder every 30 min, `connection restored`);
  signal ingestion keeps running while disconnected.
- Pins: `ib_async>=2.1,<2.2`, `pandas_market_calendars>=5.5,<6`, exact tested versions in
  `constraints.txt` (CI and Docker), gateway image pinned by tag and digest.

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
