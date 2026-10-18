# Changelog

## Unreleased

- `ibtrader dashboard`: local, read-only web dashboard (overview, trading, signals, audit, WSB
  radar tabs); loopback only, Host/Origin checks, strict CSP.
- WSB radar (`ibtrader.radar`, `ibtrader radar scan|status`), ported from the WSBRadar
  prototype: Arctic Shift / Reddit OAuth / RSS / demo sources, ticker extraction against the
  Nasdaq directory, lexical sentiment, and the daily attention features of the social-momentum
  strategy (phase 0). It emits no signals. Configured with `RADAR_*` / `REDDIT_*` keys.

- New risk limit `MAX_ENTRIES_PER_WEEK` (rule `weekly_entry_limit`, default 0 = off): caps new
  entries per calendar week (New York, all producers).
- Signal Contract 1.0, additive: optional `exit.max_holding_days` (1–250 NYSE sessions after the
  entry day); the position monitor closes with exit reason `holding_exit`. Schema regenerated.
- Producer registry: optional `paper_only_strategies` keeps listed strategies simulated (shadow)
  in live mode whatever the producer status.
- `config/producers.example.yaml`: entry for the `social-momentum` producer
  (docs/strategy/social-momentum-options.md).
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
