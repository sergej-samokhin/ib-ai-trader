# Assumptions, decisions and limitations (v0.1)

Status of this document: written against the code of v0.1. Section A records what was decided for
the open questions of the build spec (section 11). Section B lists assumptions that have to be
verified on an IBKR **paper** account before relying on the system. Section C lists known
limitations and deviations from the spec.

## A. Decisions taken for v0.1 on the spec's open questions (section 11)

Each decision below is **open for confirmation by the account owner**. None of them is a fact about
the owner's wishes; they are the defaults the code ships with.

| # | Open question | Decision in v0.1 | Where | Status |
|---|---|---|---|---|
| 1 | Approval model | Approval depends on the **producer status** (`paper_only`, `manual`, `auto`; plus `paused`) and on two global settings: `MANUAL_APPROVAL_ABOVE_USD` (an entry whose maximum premium is above it waits for a human) and `MIN_CONFIDENCE_AUTO` (an entry with lower `confidence` waits; 0.0 disables it). `manual` always waits. Approval and rejection are possible **through the CLI only** (`ibtrader approve|reject <intent_id>`); an unanswered request expires after `APPROVAL_TIMEOUT_MINUTES`. New producers should start as `paper_only` (simulated when `MODE=live`) or `manual`. | `risk.py`, `producers.py`, `app.py`, `cli.py` | open for confirmation by the account owner |
| 2 | Strategy set | **Debit only**: `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`. No credit spreads, no naked shorts. Adding a strategy requires changes to the contract, selector, risk engine and docs. | `contract/models.py` (`Strategy`), `selector.py` | open for confirmation by the account owner |
| 3 | Conflict policy | **Reject the newer signal.** A new `trade` or `rule` with the opposite direction on the same underlying as an active signal (`pending` or `active`) or an open position of a **different** producer is rejected with reason `conflict` and the operator is notified. Same-producer opposite signals are not conflicts. No netting, no priority by producer. | `gateway.py` (`_conflict`) | open for confirmation by the account owner |
| 4 | Capital and limits | **Placeholders only.** `.env.example` holds placeholder values (the code defaults are the same: `MAX_POSITION_USD=500`, `MAX_PORTFOLIO_RISK_USD=2000`, `MAX_OPEN_POSITIONS=5`, `MAX_DAILY_LOSS_USD=300`, `MAX_UNDERLYING_EXPOSURE_USD=1000`, `MIN_OPEN_INTEREST=100`, `MAX_BID_ASK_SPREAD_PCT=10`, `NO_ENTRY_OPEN_MINUTES=15`, `NO_ENTRY_CLOSE_MINUTES=15`, `MANUAL_APPROVAL_ABOVE_USD=400`). Per-producer budgets (`budget_usd`, default 500) are placeholders too. The account owner must set the real values. The number N of paper trades required before go-live is not defined either. | `.env.example`, `config.py` (`RiskLimits`), `config/producers.example.yaml` | open for confirmation by the account owner |
| 5 | Hosting | **Not decided.** A `Dockerfile` and `docker-compose.yml` (community IB Gateway image with IBC plus ibtrader) are provided as a starting point; the image must be reviewed and pinned. Region, who maintains IB Gateway and who performs the weekly re-authentication are not decided. | `docker-compose.yml`, `Dockerfile` | open for confirmation by the account owner |
| 6 | Notifications | The log channel is **always** on. **Telegram** (needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`) and **SMTP email** (needs `SMTP_HOST` and `NOTIFY_EMAIL_TO`; STARTTLS) are optional and independent. Notifications are outbound only; no commands can be given through them. | `notify.py`, `config.py` | open for confirmation by the account owner |

## B. Assumptions to verify during integration

### B.1 Items from spec section 12

| Item | State in v0.1 | How to verify |
|---|---|---|
| Market data subscriptions for US option quotes and greeks via the API | The adapter requests bid/ask, model greeks and open interest through `reqMktData`. `MARKET_DATA_TYPE` (1 live, 2 frozen, 3 delayed, 4 delayed-frozen) is configurable; the code does not check what the account is subscribed to. Missing data makes entries fail closed (liquidity rejections). Whether delayed data delivers greeks and open interest is not verified. | On the paper account, request quotes for a few symbols and check that `delta`, bid, ask and open interest arrive. |
| IBKR API pacing limits and option-chain request limits | Handled only with fixed batch sizes and caches (B.2 items 4 and 10). Not verified against the current IBKR limits. | Run a rule evaluation and an entry for a liquid underlying; watch for pacing errors in the gateway log. |
| Options trading permissions (appropriateness test completed) | Not checked by the software. A missing permission shows up as a rejected order. | Place one paper order of each strategy. |
| Tax and reporting | Out of scope for the software. | none |

### B.2 Implementation-specific assumptions

| # | Assumption | Where in code | Risk if wrong | How to verify |
|---|---|---|---|---|
| 1 | **Price tick `PRICE_TICK` = 0.05 (default) for all options and combos.** Buy limits are rounded down, sell limits up, to this tick; the minimum price is one tick. | `orders.py` (`round_buy`, `round_sell`), `config.py` | Option classes differ in their minimum price increments (some quote in 0.01, some require coarser steps above a premium threshold). A wrong tick causes rejected orders or poorer prices. Combos may use finer steps than their legs. | Check the increments for the symbols you trade in TWS and on the IBKR/OCC pages; place a paper order at an off-tick price. Adjust `PRICE_TICK` (one value for all instruments). |
| 2 | **`orderRef` format and length.** `<producer>\|<signal_id>\|v<version>\|<intent_id>`, up to about 220 characters (producer up to 64, signal id up to 128, intent id 16). Recovery matches orders by this string. | `orders.py` (`order_ref`), `ibkr.py` (`find_by_ref`) | If TWS truncates, rejects or alters the reference (length limit, the `\|` character), orders are not re-attached after a restart and become `unknown`. | Submit a paper order with a maximum-length id; read the `orderRef` back from `openTrades()`; restart the trader and check `order_reattached`. |
| 3 | **Generic ticks `100,101,106`** are requested with `reqMktData` for option quotes. The adapter waits a **fixed 2.5 seconds** (`QUOTE_WAIT_SECONDS`) before reading bid, ask, greeks and open interest, then cancels the line. Delta comes from `modelGreeks`; open interest from the call or put open-interest tick. | `ibkr.py` (`_quote_contracts`) | Data may not have arrived after 2.5 s, especially for delayed data or first requests. The result is `None` values, which the selector and risk engine treat as unusable (rejections `contract_selection` or `liquidity_*`). Open interest is updated by the exchange only once a day. | Measure how long all fields take on the paper account; compare `delta` with TWS. |
| 4 | **Strike window of 48 strikes.** For each request the 48 strikes nearest the current price (`MAX_STRIKES_PER_REQUEST` 24 times 2, in absolute distance) are qualified and quoted, in batches of 40 market-data lines (`QUOTE_BATCH`). Option quotes are cached for 30 s, the option chain for 1 hour. The chain used is the `SMART` chain whose trading class equals the symbol, otherwise the first `SMART` chain. | `ibkr.py` (`option_quotes`, `_chain`) | Deltas far from the money, or underlyings with very small strike spacing, can fall outside the window, leading to `contract_selection` rejections. Options in other trading classes are not considered. Strikes that do not exist for an expiry are dropped when qualifying. | Request a spread with a 0.10 short delta on a low-priced underlying and check the result. Inspect `reqSecDefOptParams` output for your symbols. |
| 5 | **BAG combo routing.** Spreads go out as one `BAG` contract with `exchange SMART`, one `ComboLeg` per leg (`exchange SMART`), no `smartComboRoutingParams`, and a positive limit price (net debit). Exits send the same bag with action `SELL` and a positive price (net credit). | `ibkr.py` (`IBKRBroker._contract`, `submit`) | IBKR combo price sign conventions, leg reversal on `SELL`, and leg execution guarantees were not verified. Wrong assumptions give rejected orders or wrong prices. | Place, fill and close a paper spread; compare the executions and net prices in TWS. |
| 6 | **Time in force `DAY`** for every order, entries and exits, with no outside-regular-hours flag. | `ibkr.py` (`submit`) | Exit orders expire at the close and are re-created the next session; positions are unprotected overnight and a time exit near expiry can slip by a day. | Observe order expiry on paper; decide whether exits need `GTC`. |
| 7 | **Paper account ids start with `DU`.** Used by `config.check_mode_safety` and `IBKRConnection._verify_account` to separate paper from live. | `config.py`, `ibkr.py` | A paper account with another prefix is refused in paper mode; a live account starting with `DU` cannot exist by this rule. | Check the account id in the TWS login. |
| 8 | **NYSE session calendar from `pandas_market_calendars`.** Holidays and early closes come from the package's NYSE calendar (rule-based, any year, no runtime server query). The 2026-2027 dates match the published NYSE calendar and are pinned in `tests/test_clock.py`. The NYSE equity session is used for all underlyings. | `clock.py` | Unscheduled closures (e.g. national days of mourning) are only known after a package update. Some products (for example index and ETF options) trade until 16:15, which the entry-window logic does not know. | Keep the package current; the pinned test fails if a release changes 2026-2027 dates. |
| 9 | **Market data type.** `reqMarketDataType(MARKET_DATA_TYPE)` is called once after each connect. Default 1 (live). | `ibkr.py` (`connect`), `config.py` | With delayed or frozen data (2, 3, 4) prices are stale, but orders are still placed on them; in live mode the code does not forbid it. | Set `1` for live. Confirm the subscription in the account. |
| 10 | **Pacing.** Market-data calls are synchronous: each batch waits 2.5 s, and the position monitor requests fresh quotes for every open position on every tick. Rule evaluations request historical bars each time (one request per rule evaluation). | `ibkr.py`, `app.py` | With several positions the tick takes longer than `LOOP_INTERVAL_SECONDS`; IBKR limits on simultaneous market-data lines and on historical requests may be hit. | Run with several open positions and rules on paper and watch tick duration and gateway messages. |
| 11 | **Daily bars.** `reqHistoricalData` with 1-day bars, `TRADES`, regular trading hours only, ending now, so that the last bar is assumed to be the current day's incomplete bar. `close`, `high`, `low`, `volume` therefore describe the day so far during the session, and `avg_volume_N` includes the partial day. History length requested: years = ceil(bars needed / 250). | `ibkr.py` (`daily_bars`), `indicators.py` | Conditions behave differently from what the producer expects if IBKR does not return the partial bar. | Compare `close`, `sma_20`, `rsi_14` from the audit event `rule_evaluated` with a charting tool. |
| 12 | **Contract multiplier 100.** Premium, risk, P&L and thresholds all use a multiplier of 100 (`price * 100 * quantity`). Fill prices from IBKR for combos are taken as net per-share prices. | `app.py`, `risk.py`, `store.py` | Adjusted or non-standard option contracts have other multipliers; risk and P&L would be wrong. | Trade standard contracts only; compare the booked P&L with TWS for a closed paper trade (the software ignores commissions). |
| 13 | **Order status mapping.** `Filled` is filled; `Cancelled` and `ApiCancelled` are cancelled; `Inactive` is rejected; every other status is treated as working. | `ibkr.py` (`poll`) | Unusual statuses (for example pending cancel for a long time) keep an order "working". | Exercise cancel and reject paths on paper. |
| 14 | **Recovery needs the same `IB_CLIENT_ID`.** Working orders are found through the open orders of the connection; only orders that belong to the client id are expected to be visible. `ibtrader reconcile` uses the same client id and so cannot run while the trader is connected. | `app.py` (`recover`), `ibkr.py`, `cli.py` | Orders are not re-attached; `reconcile` fails to connect. | Restart with a working order on paper. Run `reconcile` with another `IB_CLIENT_ID`. |
| 15 | **Ticker symbols.** The contract pattern allows tickers such as `BRK.B`. The adapter builds `Stock(symbol, "SMART", "USD")` with the symbol unchanged. IBKR may write share-class symbols differently (for example with a space). | `contract/models.py` (`Ticker`), `ibkr.py` (`_stock`) | Signals for such underlyings are accepted but fail at contract qualification. | Test every non-trivial ticker on paper. |
| 16 | **Docker relay ports and IBC.** The gateway image relays the API on 4003 (live) and 4004 (paper); this is confirmed by the image README (2026-10-18). The image bundles IBC, which was **retired and archived on 2026-09-01** (see [RELATED_PROJECTS.md](RELATED_PROJECTS.md)). | `docker-compose.yml` | An image change breaks connectivity; an IBKR Gateway change can break the unmaintained IBC login automation. | Pin the image digest; test the connection on paper; decide the login strategy under open question 5. |
| 17 | **Quotes.** A quote is two-sided when bid is not negative and ask is positive; mid is the average. IBKR "no data" values (`-1`, NaN) are mapped to missing. | `domain.py`, `ibkr.py` (`_num`) | A genuine bid of 0 is kept (valid), a missing one is not distinguishable from a stale one. | Observe quotes for far out-of-the-money options. |

## C. Known limitations of v0.1

Verified against the code. Items marked **(spec)** deviate from, or only partly meet, the spec.

### Control and operations

- **Kill switch is a file flag and a CLI command only.** There is no Telegram or other notifier
  command **(spec: "file flag or notifier command")**. Notifications are outbound only.
- **The kill switch cancels working and pending entry orders, not exits.** Exit orders and the
  position monitor continue. The spec's section 6 wording is "cancel all open orders"; section 5
  says exits are allowed with the kill switch active, and the code follows section 5.
- The kill switch, like the rest of the tick, is not processed while the IBKR connection is down;
  a failed reconnect skips the tick.
- **Approvals are CLI only.** No approval by Telegram or email; the requests are sent as notifications.
- **A paused producer's `exit` documents are rejected** (`producer_paused`), while its position
  monitoring continues. Pausing does not cancel intents already approved, awaiting approval or working.
- **No heartbeat, watchdog or metrics.** Repeated `Loop error` notifications are not de-duplicated.
- **Single process, single thread.** One loop does ingestion polling, evaluation, order management
  and monitoring. Market-data calls block it (about 2.5 s per quote batch). SQLite, no high availability.
  HTTP requests are handled in extra threads that only call the gateway.
- Producer registry and inbox/outbox directories are created and read **at startup only**; adding a
  producer, changing a status or revoking an API key requires a restart (`producer pause` is
  immediate).
- Log files are not rotated; `outbox/`, `processed/` and `quarantine/` are never cleaned.
- `ibtrader run --demo` stores its state under `DATA_DIR/demo/` but reads the real `config/producers.yaml`.

### Positions, recovery and reconciliation

- **No automatic import of pre-existing IBKR positions**, and no command to adopt, close or forget a
  position in the store. `ibtrader reconcile` is read-only and compares options only. Positions that
  exist at IBKR but not in the store are never managed (no exits).
- **Recovery does not detect fills that happened while the trader was down.** Orders that are no
  longer open at IBKR become `unknown`; entry fills are then not recorded as positions, and an exit
  fill leaves the position open in the store. An `unknown` *exit* intent blocks further exits for that
  position until the operator reconciles it (so a position is never sold twice); an `unknown` *entry* is
  counted as open risk (conservative).
- After a rejected or failed exit order (no fill) the monitor waits 300 s (`EXIT_RETRY_BACKOFF_SECONDS`
  in `app.py`) before creating a new one; it retries indefinitely at that pace.
- Simulated (shadow) orders are dropped at restart; their positions are kept.
- Exercise and assignment are not handled. Positions are expected to be closed by the
  `close_before_expiry_days` rule.
- Realised P&L ignores commissions and fees. The daily loss limit uses realised P&L only (no unrealised
  losses) and the New York calendar day. Shadow fills are simulated at the limit price with no slippage
  or partial fills.
- Premiums and P&L use a contract multiplier of 100 (B.2 item 12).
- The scorecard mixes real and simulated positions in `trades`, `closed`, `wins` and `hit_rate`; it counts
  accepted exit signals in `accepted` and does not show duplicates, expired or rate-limited documents separately.
- Only US equity options on `SMART`; the session calendar is the NYSE equity calendar (B.2 item 8).

### Signals and the contract

- **Strike selection is by delta only.** Moneyness-based selection (mentioned in the spec's component
  description) is not implemented. The selector does not skip illiquid strikes; the liquidity filter runs after the choice. Only the 48 strikes nearest the price are considered.
- **Debit strategies only** (open question 2). Each spread leg has ratio 1.
- **`confidence` is not used for sizing** **(spec: "may feed sizing")**. It only drives the optional approval threshold `MIN_CONFIDENCE_AUTO`.
- **`max_entries_per_day` counts rule firings, not fills**, and a rule has no notion of a position it already holds.
  A `check_at` outside the entry window fires and is rejected (`market_hours`) and still uses the day's entry.
  The spec's own example `check_at: "15:45 America/New_York"` is outside the default entry window (the window end
  is exclusive at 15:45 with `NO_ENTRY_CLOSE_MINUTES=15`), so such a rule is rejected every day with default settings.
- **An `exit` signal does not retire a rule**; it only closes positions and cancels unfilled entries.
- A `trade` is consumed at its first attempt, whatever the outcome; it waits for the entry window while pending.
- An exception during one rule's evaluation (for example a market-data error) aborts the evaluation of the remaining rules in that tick and
  repeats on the next tick.
- Validation messages for cross-field rules use the field `$` with the field name in the message.
- The reason code `immutable` and the rule id `signal_max_contracts` (constant `SIGNAL_SIZING`) exist in the code but are
  unreachable today.
- Feedback to producers has gaps: no events for manual approval, approval timeout, order submission errors, position
  opened or closed, or `unknown` orders; entry and exit order events are not distinguished, and simulated orders look like real ones.
  HTTP producers must poll (`GET /v1/signals/<id>`); there is no push.
- The `conflict` rejection names the other producer's signal id; conflicts are only evaluated at receipt, against other
  producers' active signals and open positions (not against working order intents).

### Ingestion and security

- **The HTTP adapter has no TLS** (localhost bind or reverse proxy), no payload signing (HMAC), no socket timeouts, and no
  audit trail or rate limit for failed authentication. Only `Content-Length` bodies are read; an oversized payload gets `422 payload_too_large`, not `413`.
- File-drop permissions are the operator's responsibility; the software does not set or check them.
  Documents from a directory that is not a registered producer are stored in `raw_signals` and rejected before the rate limit.
- "Verbatim" storage of payloads is UTF-8 decoded with replacement characters; payloads over `MAX_SIGNAL_BYTES` are stored truncated.
- The audit log is append-only through database triggers; someone with write access to the database file can still drop the triggers.

### Testing and quality

- `src/ibtrader/ibkr.py` has **no automated tests** and has not been validated against a paper account;
  the same holds for the CLI (`cli.py`) and the real Telegram and SMTP notifiers. All other tests use a simulated broker
  and synthetic market data. The integration, adversarial and failure-drill items of the spec's section 9 that involve IBKR have to be performed
  by hand (README, integration checklist).
- The stack differs from the spec's suggestion in one point: scheduling is a plain loop (`LOOP_INTERVAL_SECONDS`), not `APScheduler`.
