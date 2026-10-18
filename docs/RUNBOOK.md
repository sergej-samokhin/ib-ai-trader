# Runbook

Operations guide for `ibtrader` v0.1. It describes what the code does today. Where behaviour is
surprising or a gap exists, it says so; the consolidated list is in
[ASSUMPTIONS.md](ASSUMPTIONS.md#c-known-limitations-of-v01).

Contents

1. [Conventions](#1-conventions)
2. [Prerequisites](#2-prerequisites)
3. [Start and stop](#3-start-and-stop)
4. [Health checks](#4-health-checks)
5. [Kill switch](#5-kill-switch)
6. [Pausing a producer](#6-pausing-a-producer)
7. [Manual approvals](#7-manual-approvals)
8. [Crash recovery](#8-crash-recovery)
9. [Reconciling with IBKR](#9-reconciling-with-ibkr)
10. [IB Gateway re-authentication and IBC](#10-ib-gateway-re-authentication-and-ibc)
11. [Daily routine](#11-daily-routine)
12. [Promoting a producer](#12-promoting-a-producer)
13. [Switching paper → live](#switching-paper--live)
14. [Incident playbooks](#14-incident-playbooks)
15. [Where data lives](#15-where-data-lives)
16. [SQL queries for the audit log](#16-sql-queries-for-the-audit-log)

---

## 1. Conventions

- Commands are run from the repository root, where `.env` and `config/producers.yaml` are found.
  `ibtrader --env-file /path/to/.env <command>` selects another env file (the option comes before the command).
- All CLI commands read the same `DATA_DIR` (default `var/`) as the running trader. They talk to
  the trader only through the SQLite database and the files in `DATA_DIR`; there is no control socket.
- With docker compose, run CLI commands inside the container so that the environment matches:
  `docker compose exec ibtrader ibtrader status`. The container runs as user `trader` (uid 1000);
  the host directory `./var` must be writable by that uid.
- Timestamps in the database and logs are UTC. Trading-day logic (entry window, daily loss, rule
  `check_at`, daily summary) uses America/New_York.
- Intent ids look like `int_<12 hex>`, position ids like `pos_<12 hex>`.

## 2. Prerequisites

- Python 3.12 or newer (`pip install -e ".[dev]"`), or docker compose.
- `.env` copied from `.env.example` and edited. The risk limits in `.env.example` are placeholders;
  the account owner must set the real values.
- `config/producers.yaml` copied from `config/producers.example.yaml` with at least one producer.
  `ibtrader run` exits with code 2 and the log line `no producers registered` if the file is missing or empty.
  The file is read once at startup. Adding or changing a producer needs a restart.
- IB Gateway (or TWS) running, logged in, API enabled and not read-only, listening on `IB_HOST`:`IB_PORT`.
  Options trading permission and market data for US stocks and options on the account
  (README, "Setup with IBKR").
- Producers can write to their inbox (file drop) or have an API key (HTTP).
  See [ADR 0001](adr/0001-signal-ingestion.md) for the permission layout.
- The assumptions in [ASSUMPTIONS.md](ASSUMPTIONS.md#b-assumptions-to-verify-during-integration)
  verified on a **paper** account. The IBKR adapter (`src/ibtrader/ibkr.py`) has no automated tests.

## 3. Start and stop

### Start

```bash
ibtrader run                 # connects to IBKR, runs until SIGTERM/SIGINT
ibtrader run --demo          # synthetic market data, simulated broker, no IBKR connection
ibtrader run --once          # one loop iteration, then exit (smoke test)
docker compose up -d         # IB Gateway (with IBC) plus ibtrader
```

Startup sequence of `ibtrader run`:

1. Settings load and logging starts (stderr and `var/logs/ibtrader.jsonl`).
2. `config.check_mode_safety` runs. On any problem the process logs `unsafe configuration` and exits
   with code 2 (not with `--demo`). See [Switching paper → live](#switching-paper--live).
3. The producer registry is loaded. An empty registry exits with code 2.
4. Connection to IBKR (up to 5 attempts, waits of 2, 4, 8 and 16 seconds). After connecting,
   `IBKRConnection._verify_account` checks `IB_ACCOUNT` against `MODE`; a mismatch raises an error immediately and the process exits.
5. `Trader.recover` (see [Crash recovery](#8-crash-recovery)).
6. With file drop enabled, inbox and outbox directories are created for every registered producer; the HTTP adapter starts if `HTTP_ENABLED=true`.
7. Notification `ibtrader started`, then the loop: every `LOOP_INTERVAL_SECONDS` (default 5) it ensures the IBKR
   connection, polls the file-drop inbox and runs one `tick`. Each tick runs, in this order: kill switch,
   expire signals, trade signals, exit signals, rule evaluation, approvals, order management, position monitor, daily summary.
   An exception in one step is logged, audited as `error` and notified; the other steps still run.

Demo mode keeps its state under `DATA_DIR/demo/` (own database, inbox, outbox and kill flag), so
simulated positions never mix with real ones. It still reads the real `config/producers.yaml`.

### Docker compose notes

- `docker-compose.yml` needs `TWS_USERID` and `TWS_PASSWORD` in the environment or `.env`. Keep the gateway's
  `TRADING_MODE` (default `paper`) consistent with ibtrader's `MODE`.
- Inside the compose network the API relays are on ports 4004 (paper) and 4003 (live), per the compose file.
  The default `IB_PORT=4002` in `.env.example` does not fit compose. Set `IB_PORT=4004` (paper) or `4003`
  (live) in `.env`; the compose file passes `${IB_PORT:-4004}` to ibtrader, and docker compose reads `.env`
  for that substitution.
- `config.check_mode_safety` treats 4004 as a paper port and 4003 as a live port, so the relay ports are
  covered by the port-versus-mode check as well.
- `restart: unless-stopped` restarts ibtrader after a crash. Every restart runs recovery.

### Stop

```bash
# Ctrl-C in the foreground, or:
kill -TERM <pid>
docker compose stop ibtrader
```

SIGTERM and SIGINT set a flag. The loop finishes the current iteration, then stops the HTTP adapter,
disconnects from IBKR and sends the notification `ibtrader stopped` (exit code 0). The iteration can take several
seconds because market-data calls block; `docker compose stop` waits 10 seconds by default (no
`stop_grace_period` is set in the compose file) and then kills the container.

What a stop does **not** do:

- Working orders stay at IBKR (orders are DAY orders and expire at the close).
- Nothing watches open positions while the trader is down: no take profit, stop loss or time exit.

Before a planned stop with entry orders working: `ibtrader kill on --reason "planned stop"`, wait until
`kill_switch_activated` appears in the audit log (one loop iteration), then stop.

## 4. Health checks

There is no heartbeat, metrics endpoint or watchdog in v0.1. Use these indirect signs.

| Check | How |
|---|---|
| Process alive | `pgrep -f "ibtrader run"`, or `docker compose ps` |
| Errors and warnings | `grep -F -e '"level": "error"' -e '"level": "warning"' var/logs/ibtrader.jsonl \| tail -n 20` (or `jq -c 'select(.level=="error" or .level=="warning")'`) |
| IBKR connection | log messages `connected to IBKR`, `IBKR disconnected, reconnecting`, `IBKR connect failed`; notification `Loop error` |
| State | `ibtrader status` |
| Liveness over time | `sqlite3 var/ibtrader.sqlite3 "SELECT * FROM kv"`: `summary_date` is set to the New York date each trading day after the daily summary time; `kill_switch_active` (present after the first kill switch change) shows `1` or `0` |
| HTTP adapter (if enabled) | `curl -s http://127.0.0.1:8787/healthz` returns `{"ok": true}`. It proves only that the HTTP thread runs, not the trading loop. |

Log format: one JSON object per line with `ts`, `level`, `logger`, `msg` and event fields. Decisions are
also in the `audit_log` table (section 16).

`ibtrader status` prints JSON with `mode`, `kill_switch` (boolean), `open_positions`, `open_intents`
(statuses `awaiting_approval`, `approved`, `submitted`, `unknown`) and `active_signals` (status `pending`
or `active`). It does not show whether the trader process is running or connected.

Notifications (always logged; Telegram if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set; email if
`SMTP_HOST` and `NOTIFY_EMAIL_TO` are set, always with STARTTLS):

| Subject | Level | Meaning |
|---|---|---|
| `ibtrader started` / `ibtrader stopped` | info | process start, clean shutdown |
| `KILL SWITCH ACTIVE` / `Kill switch released` | critical / warning | see section 5 |
| `Approval needed: <intent_id>` / `Approval expired` | warning / info | see section 7 |
| `Rejected: <underlying> <strategy>` | warning | entry rejected, with rule id |
| `Signal conflict` | warning | conflict policy rejected a newer signal |
| `Filled` / `Exit filled` (suffix `[SHADOW]` for simulated) | info | fills and realised P&L |
| `Exit not yet filled` | warning | exit order is working at the natural price |
| `Order submission failed` | error | broker call raised; intent status `error` |
| `Order state unknown after restart` | error | see section 8 |
| `IBKR connection lost` / `IBKR still disconnected` / `IBKR connection restored` | critical / critical / warning | see section 10 |
| `Position closed manually` | warning | `ibtrader position close` was used |
| `Ingestion error` | error | a transport adapter raised while polling |
| `Loop error`, `Trader error` | error | exception in the loop or in a tick step |
| `Daily summary <date>` | info | once per trading day |

## 5. Kill switch

```bash
ibtrader kill on --reason "unexpected fills"   # write var/KILL
ibtrader kill status                           # {"kill_switch": "active"|"off", "reason": ...}
ibtrader kill off                              # remove var/KILL
```

The switch is the file `<DATA_DIR>/KILL` (content: UTC time and reason). `touch var/KILL` also works; the reason
then shows as `(no reason given)`. There is no Telegram or other remote command.

**What happens.** The trader checks the file on every tick. On the change to active it:

- cancels all open **entry** intents: those with a working order get a broker cancel (final status
  `cancelled`, or `partially_filled` if some quantity filled); those still `awaiting_approval` or `approved` become
  `cancelled` (rule `kill_switch`);
- writes the audit event `kill_switch_activated` (with `entries_cancelled`) and sends the critical notification `KILL SWITCH ACTIVE`.

While it is active:

- Every new entry is rejected with rule `kill_switch`: trade signals (they are consumed and rejected, not held),
  rule firings (each uses one of the rule's daily entries) and approved intents about to be submitted.
- **Exits continue**: the position monitor (take profit, stop loss, time exit) and exit signals still work, and exit orders are not cancelled.
- Signals are still accepted by the gateway. Producers are not paused (see section 6).

What it does **not** do: close positions, cancel exit orders, stop the process, pause producers. Quantity
that filled before the cancel becomes a normal position with exit rules.

Caveats:

- It only works while the loop runs and the IBKR connection is up. If the connection is down, the loop
  skips the whole tick (including the kill switch step) until it is back.
- If you need orders gone at once, also cancel them in TWS or IB Gateway. Orders carry the `orderRef`
  `<producer>|<signal_id>|v<version>|<intent_id>`.
- `kill off` allows new entries again at the next tick (notification `Kill switch released`). Cancelled intents
  and trade signals consumed during the kill are not revived.

Verify: `ibtrader kill status`, then the audit query for `kill_switch_activated` (section 16), then `ibtrader status` for remaining `submitted` intents.

## 6. Pausing a producer

```bash
ibtrader producer pause momentum-agent --reason "bad data feed"
ibtrader producer resume momentum-agent
ibtrader producer list
```

`pause` and `resume` write the `producer_pauses` table and the audit events `producer_paused` and
`producer_resumed`. They take effect immediately, without a restart. The output lists the producers of
`producers.yaml` with `status`, `budget_usd` and `paused`. **The command does not check that the id
exists**: a typo pauses nothing. Confirm `"paused": true` in the output.

Effects:

- New documents from the producer are rejected with `producer_paused` (exit documents included).
- Entries from the producer are rejected by the risk engine with rule `producer_paused`. This includes
  pending trade signals and active rules that fire.
- Not affected: intents that are already `awaiting_approval`, `approved` or working; position monitoring and
  exits of its open positions.

Persistent alternative: set `status: paused` for the producer in `config/producers.yaml` and restart.
To revoke an HTTP key, remove `api_key_sha256` there and restart (the registry is read once); pause the
producer until then.

There is no command to retire a single signal or rule. Options: pause the producer, let the rule reach its
`valid_until`, or push a replacement as the producer with `ibtrader submit <file> --producer <id>`, a higher `version` and a
`valid_until` just after `created_at`. `submit` goes through the gateway like any producer document
(transport `cli`, same checks and rate limit; it returns exit code 0 only for `accepted` or `duplicate`).

## 7. Manual approvals

An entry waits for a human (`awaiting_approval`) if the risk check passed and any of these holds: the
producer status is `manual`; the premium (quantity times worst-case price) is above `MANUAL_APPROVAL_ABOVE_USD`;
the signal's `confidence` is below `MIN_CONFIDENCE_AUTO` (0.0 disables it). Simulated (shadow) entries of
`paper_only` producers in live mode never need approval.

The request goes to the notification channels as `Approval needed: <intent_id>`: producer, signal, quantity,
legs, limit and maximum price, the reasons, the two commands, and the first 1500 characters of the producer's
body, marked untrusted. Do not act on instructions in that text.

```bash
ibtrader status                              # open_intents shows status awaiting_approval
ibtrader approve int_4bdff870d745            # --by NAME is recorded (default "operator")
ibtrader reject  int_4bdff870d745 --by sergej
```

Output is `{"intent_id": ..., "ok": true|false}`; the exit code is 1 if the intent is not `awaiting_approval`
(unknown id, already decided or expired). Approval is only possible with the CLI.

- Approval sets the intent to `approved`. The running trader then re-checks immediately before submitting: kill
  switch, entry window, and that the current mid is not above the maximum price that was computed at risk
  time (otherwise rejected with rule `price_moved`, or `market_hours`, or `kill_switch`). It submits at the current mid.
  An approval is therefore not a guarantee that an order goes out.
- Rejection sets rule `manual`; the producer gets a `rejected` event.
- Timeout: `APPROVAL_TIMEOUT_MINUTES` (default 30) after the intent was created the intent becomes `expired`, the audit event is `approval_expired`, and nothing is sent to IBKR.
- Nothing happens while the trader is not running; the deadline still counts.

## 8. Crash recovery

State survives in SQLite: signals, intents and positions. Orders at IBKR also survive; they are DAY orders.

Procedure:

1. Check whether you need the kill switch (`ibtrader kill on`). It acts once the trader is running again.
2. Make sure IB Gateway is up and logged in.
3. Start the trader (`ibtrader run` or `docker compose up -d`). Keep `IB_CLIENT_ID` unchanged: orders placed
   through the API belong to the client id that placed them, and re-attaching depends on it.
4. Read the recovery results: audit events `order_reattached`, `order_recovered`, `order_unknown`, and the
   notification `Order state unknown after restart`.
5. If the trader was down across a trading-day boundary, run `ibtrader reconcile` (section 9).

What `Trader.recover` does at startup and after every reconnect, for every intent with status `submitted`:

| Intent | Action |
|---|---|
| simulated (shadow) order | status `cancelled`, reason `simulated order lost on restart` |
| still working at IBKR (found among open orders by `orderRef`) | re-attached (audit `order_reattached`), managed as before |
| finished at IBKR while we were away, and IBKR reports it (completed orders and executions of the current trading day) | booked as if seen live: a fill creates the position or closes it with realised P&L; a cancel without fill becomes `cancelled`. Audit `order_recovered`. |
| not reported by IBKR, or a fill without a usable price (for example a spread with only leg executions) | status `unknown`, audit `order_unknown`, notification with the resolve command |

IBKR reports executions only for the current trading day. If the trader was down overnight, fills from the
previous day are not visible and those intents become `unknown`.

Intents in `awaiting_approval` and `approved` carry on (approval deadlines are stored).

**Resolving `unknown` intents.** Nothing retries an `unknown` intent. An `unknown` exit blocks further exits for
that position, and an `unknown` entry counts as open risk, until you resolve it. Look the order up in TWS (or the
IBKR activity report) by its `order_ref` (query in section 16), then:

```bash
ibtrader intent resolve int_xxxxxxxxxxxx --filled 0                 # did not fill
ibtrader intent resolve int_xxxxxxxxxxxx --filled 2 --price 2.45    # filled (net price per share for spreads)
```

| What happened at IBKR | Effect of `intent resolve` |
|---|---|
| Entry not filled | intent `cancelled`; no longer counts as open risk |
| Entry filled (fully or partly) | position created with the given quantity and price; exits are managed from now on |
| Exit filled | position closed or reduced; realised P&L booked (daily loss limit, scorecard) |
| Exit not filled | intent `cancelled`; the monitor creates a new exit on its next run |

Each resolution is audited as `manual_intent_resolved` with `--by` (default `operator`).

## 9. Reconciling with IBKR

```bash
ibtrader reconcile
```

The command connects to IBKR (it needs a running IB Gateway), compares positions, prints JSON and exits with
code 0 if everything matches and 1 if not. It changes nothing.

`reconcile` connects with `IB_CLIENT_ID`, the same id as the trader. The TWS API does not allow two
connections with one client id (not tested with this code), so either **stop the trader first** or run it
with another id: `IB_CLIENT_ID=18 ibtrader reconcile`. With docker compose:
`docker compose stop ibtrader && docker compose run --rm ibtrader reconcile`, or
`docker compose run --rm -e IB_CLIENT_ID=18 ibtrader reconcile` while it is running.

Output:

```json
{"differences": {"123456789": {"expected": 2.0, "broker": 0.0}, "123456790": {"expected": -2.0, "broker": 0.0}}}
```

- Keys are IBKR contract ids (`conId`). `{"differences": {}}` means no differences.
- `expected` comes from the store: the sum over open, non-simulated positions of signed leg quantities
  (BUY legs positive, SELL legs negative). A 2-lot bull call spread gives `+2` for the long call's `conId`
  and `-2` for the short call's `conId`.
- `broker` is the signed position of option contracts in the account (`IB_ACCOUNT`, or the only account).
- Only option positions are compared; shadow positions are excluded.

What a difference means and what to do:

| Pattern | Likely cause | Action |
|---|---|---|
| broker has more than expected, or contracts the store does not know | entry filled while `unknown`; manual trade; options that existed before ibtrader (v0.1 does not import or ignore them) | Find the source in TWS. Close manually or accept manual management. If it keeps showing, use a dedicated account. |
| broker has less than expected, or nothing | exit filled during downtime; position expired, was assigned or closed manually | Close the position in the store (below). |
| sign or quantity differences on one spread | partial fill or one leg changed manually | Inspect `positions.legs` for the position (section 16) and compare with TWS; fix at the broker. |

v0.1 never changes the store or the broker from `reconcile`; it does not auto-import positions.
Look up a `conId` in your TWS portfolio view or in `positions.legs`:
`sqlite3 var/ibtrader.sqlite3 "SELECT id, producer, signal_id, legs FROM positions WHERE status='open'"`.

When to run it: after every restart with `unknown` intents or after a crash, after any manual action in
TWS, and daily after the close (section 11).

### Correcting the store

If a position was closed outside ibtrader (for example by hand in TWS), book the close:

```bash
ibtrader position close pos_xxxxxxxxxxxx --price 0.85              # all contracts, net credit per share
ibtrader position close pos_xxxxxxxxxxxx --price 0.85 --quantity 1 # partial
```

This writes the realised P&L (so the daily loss limit and the scorecard stay right), cancels `unknown` or
not-yet-submitted exit intents of that position, audits `manual_position_closed` and sends a notification. It
refuses while an exit order for the position is still working at IBKR: cancel that order in TWS first and let
the trader process the cancel. Then run `ibtrader reconcile` again.

Positions that exist at IBKR but not in the store (opened outside ibtrader) still cannot be imported. Manage
them in TWS, or use a dedicated account for ibtrader.

## 10. IB Gateway re-authentication and IBC

The software does not log in to IBKR. IB Gateway (or TWS) must be logged in, and IBKR requires fresh
authentication regularly, including a second factor, at a frequency set by IBKR (about weekly; check the
current IBKR and IBC documentation). IBC automates the login and the daily restart; the compose file sets
`AUTO_RESTART_TIME` to `11:59 PM`, `TIME_ZONE` to America/New_York and `TWOFA_TIMEOUT_ACTION` to `restart`. Someone must be
named to complete the second factor (spec open question 5, see [ASSUMPTIONS.md](ASSUMPTIONS.md)).

**IBC is retired** (archived 2026-09-01, only critical fixes). It still works inside the gateway image, but
has no maintained upstream; options and the fork under evaluation are listed in
[RELATED_PROJECTS.md](RELATED_PROJECTS.md#ibcalphaibc-retired). Alert on disconnects so a broken auto-login is noticed.

When the gateway is down or not yet authenticated:

- Each loop iteration tries to reconnect (5 attempts with 2, 4, 8 and 16 second waits).
- Alerts: one `IBKR connection lost` (critical) per outage, a reminder `IBKR still disconnected` every 30
  minutes, and `IBKR connection restored` when it is back. Audit events `connection_lost` and
  `connection_restored`.
- While disconnected the trading tick is skipped: no entries, exits, approvals, repricing or kill-switch
  processing (no order actions without a broker). Signal ingestion keeps running (file drop and HTTP), and
  signals are acknowledged as usual.
- Orders already at IBKR stay there.
- When the connection returns, cached order handles are dropped and recovery runs (section 8) before the
  next tick: working orders are re-attached, orders that finished in the meantime are booked.

Plan the weekly authentication outside the session. Afterwards check the log for `connected to IBKR`, then
`ibtrader status`.

## 11. Daily routine

Before the open:

1. Gateway logged in; trader running (section 4); `ibtrader status` shows no unexpected `unknown` or stale `awaiting_approval` intents; kill switch off.
2. Errors since yesterday (section 4, section 16).

During the session: answer approval requests (section 7); watch notifications.

After the close:

1. The daily summary arrives at `DAILY_SUMMARY_TIME` (New York time, default `16:15`, trading days, once a day; if the
   trader was down at that time it is sent at the first tick afterwards). It contains the mode, realised P&L of the day
   (and shadow P&L), open positions with premium at risk, intents of the day by status, and the kill switch state.
2. `ibtrader scorecard` (section 12).
3. `ibtrader reconcile` (stop the trader or use another client id, section 9).
4. Review rejections and errors (section 16).

Weekly: back up the database (`sqlite3 var/ibtrader.sqlite3 ".backup var/backup-$(date +%F).sqlite3"`), tidy
`var/outbox`, `var/processed` and `var/quarantine` (no automatic cleanup), review the scorecard, check the weekly gateway login.
Keep `pandas_market_calendars` up to date (`pip install -U pandas_market_calendars`) so newly announced market closures are known; it computes the NYSE calendar locally and does not fetch updates at runtime.

## 12. Promoting a producer

Statuses (`config/producers.yaml`, applied after a restart):

| Status | `MODE=paper` | `MODE=live` |
|---|---|---|
| `paper_only` | trades on the paper account (approval thresholds still apply) | simulated only (shadow); never sent to IBKR |
| `manual` | every entry needs `ibtrader approve` | same, real orders |
| `auto` | executes within limits | executes within limits; entries above `MANUAL_APPROVAL_ABOVE_USD` or below `MIN_CONFIDENCE_AUTO` still need approval |
| `paused` | all documents rejected | same |

Go-live criteria from the spec (section 9): a minimum number N of paper trades per producer without unhandled
errors, plus a manual review of the audit log. **N is not defined** in the spec or the code; the account owner sets it (open item in
[ASSUMPTIONS.md](ASSUMPTIONS.md)). Suggested evidence before each step (`paper_only` to `manual`, then `manual` to `auto`):

1. `ibtrader scorecard` per producer (output is a list of objects):

   | Field | Meaning |
   |---|---|
   | `received` | all documents received, any outcome (duplicates, expired and rate-limited included) |
   | `accepted`, `rejected` | documents with these acknowledgement statuses (accepted exit signals count in `accepted`) |
   | `trades`, `closed`, `wins` | positions opened, closed, and closed with positive realised P&L; shadow positions are included |
   | `hit_rate` | `wins / closed`, or `null` |
   | `pnl`, `shadow_pnl` | realised P&L in USD of real and of simulated positions |

   Notes: P&L excludes commissions and fees. Simulated fills happen at the limit price as soon as it crosses the current mid, without slippage or
   partial fills, so `shadow_pnl` is optimistic. `trades`, `closed` and `hit_rate` mix real and simulated positions.
2. Rejections by rule for the producer (section 16); a high share of rejections points at signals that are out of line with the limits.
3. No rows from the error query (`error`, `order_error`, `order_unknown`, `exit_unpriced`) for the period, and no `unknown` intents.
4. Manual audit review: `raw_signals` payloads and `audit_log` for the producer; check that nothing odd was in the bodies.
5. For `manual` to `auto`: look at the approvals you gave and rejected.

Edit `status` (and `budget_usd` if needed), restart, and check the startup log.

## Switching paper → live

Go through every item. Do not switch before the paper integration checklist in the README and the items in
[ASSUMPTIONS.md](ASSUMPTIONS.md#b-assumptions-to-verify-during-integration) are done.

1. Set the global limits deliberately in `.env` (`MAX_POSITION_USD`, `MAX_PORTFOLIO_RISK_USD`, `MAX_OPEN_POSITIONS`,
   `MAX_DAILY_LOSS_USD`, `MAX_UNDERLYING_EXPOSURE_USD`, `MIN_OPEN_INTEREST`, `MAX_BID_ASK_SPREAD_PCT`,
   `NO_ENTRY_OPEN_MINUTES`, `NO_ENTRY_CLOSE_MINUTES`, `MANUAL_APPROVAL_ABOVE_USD`, `MIN_CONFIDENCE_AUTO`) and each producer's `budget_usd`. The placeholders are not recommendations.
2. Keep every producer at `paper_only` (simulated in live mode) or `manual`. Promote only per section 12.
3. Gateway: log in to the **live** account (compose: `TRADING_MODE=live`).
4. `.env`:
   - `MODE=live` (the value is case-insensitive; anything other than `paper` or `live` is a configuration error)
   - `IB_PORT` = the live port: IB Gateway 4001, TWS 7496 (compose relay: 4003)
   - `IB_ACCOUNT` = the live account id (required; must not start with `DU`)
   - `MARKET_DATA_TYPE=1` (live data). The code does not stop you from using delayed data (`3`) in live mode; do not.
5. Start. The startup checks refuse inconsistent settings:
   - `config.check_mode_safety` (logged as `unsafe configuration`, exit code 2): `MODE=paper` with port 4001 or 7496; `MODE=live`
     with port 4002 or 7497; `MODE=live` without `IB_ACCOUNT`; `MODE=paper` with an `IB_ACCOUNT` that does not start with `DU`.
   - `IBKRConnection._verify_account` after connecting (error and exit, no retry): the account (`IB_ACCOUNT`, or the single managed account) must be
     among the managed accounts; `MODE=paper` requires an account starting with `DU`; `MODE=live` rejects an account starting with `DU`.
   - The compose relay ports count as paper (4004) and live (4003). Other custom ports are not covered by the port check.
6. Watch the first session: keep a terminal ready for `ibtrader kill on`. Expect that the first real entries need
   approval if the producer is `manual`.
7. Rollback: `ibtrader kill on`, set `MODE=paper` with the paper port and account, restart.

## 14. Incident playbooks

First move in any doubt: `ibtrader kill on --reason "<why>"`, then investigate. It blocks new entries; exits continue.

### IB Gateway disconnected

Symptoms: notification `IBKR connection lost`, log `IBKR disconnected, reconnecting`. Consequences: section 10.
Fix the gateway (login, second factor, restart; with IBC check its log). The loop reconnects by itself and
runs recovery. Afterwards run `ibtrader status`; resolve any `unknown` intents (section 8); if the outage
crossed a trading-day boundary, run `ibtrader reconcile`.

### Order rejected by IBKR

An entry order that IBKR rejects or deactivates ends with status `rejected` (reason: the broker message),
the producer gets `order_rejected`, and nothing retries. An exception when submitting gives status `error` and the notification `Order submission failed`.
Find the cause in the intent's `reason` and in TWS messages (permissions, market data, tick size, contract, account). Common suspects are in
[ASSUMPTIONS.md](ASSUMPTIONS.md) section B (tick size, `orderRef`, combo routing).

**Rejected exits are retried every 5 minutes.** If an exit order is rejected or fails without a fill, the monitor
waits 300 s and then creates a new exit intent. Detect repeated attempts with the exit-retry query (section 16).
Fix the cause, close manually if needed, and correct the store if the position was closed outside ibtrader (section 9).

### Stuck exit

Symptoms: `Exit not yet filled`, an exit intent in status `submitted` for a long time.

- The exit limit starts at the closing mid and moves toward the natural price (bid for long legs) in `EXIT_MAX_REPRICE_STEPS` steps (default 5, one per
  `REPRICE_INTERVAL_SECONDS`), then keeps following the natural price, with no further notifications.
- The order is a DAY order. If it does not fill, IBKR cancels it at the close, and the monitor creates a new exit during the next session (the monitor only runs during regular hours).
- `exit_unpriced` in the audit log means no two-sided quote exists, so no exit order is created at all; it repeats on each tick.
- Intervene in TWS if the market has no bid: cancel and close manually, then `ibtrader reconcile` and correct the store.
- Positions close to expiry must not be left alone: ibtrader does not handle exercise or assignment.

### Unexpected position at IBKR

`ibtrader reconcile` shows `broker` larger than `expected`. Possible sources: an `unknown` entry that filled,
a manual trade, another API client, exercise or assignment. ibtrader does not manage such a position. Decide to
close it manually or keep it; sending an `exit` signal does not help because it acts only on positions the store knows.
Also check `kill on` if the cause is unclear.

### A producer misbehaves (flood, bad signals, suspicious content)

`ibtrader producer pause <id> --reason ...`. Inspect `raw_signals` and the quarantine folder (section 16). The document
body is never executed, and risk limits do not depend on the signal, but the body may have been forwarded to your notification
channels if an approval was requested. A flood is limited by `rate_limit_per_hour`; see the rate-limited rows in
the gateway-outcome query. Review audit entries `signal_rejected` for the producer.

### Loop or step errors

`Loop error` or `Trader error` notifications with `step` names. A failing step is retried on the next tick. A rule evaluation that raises (for example a
market-data error) stops the evaluation of the remaining rules in that tick and is retried at the next tick. For persistent errors read
`var/logs/ibtrader.jsonl` (field `exc`) and the `error` events in the audit log.

### Database problems

`sqlite3 var/ibtrader.sqlite3 "PRAGMA integrity_check"`. Restore from the last backup (section 11) with the trader stopped, then run `ibtrader reconcile`.

## 15. Where data lives

Everything is under `DATA_DIR` (default `var/`, git-ignored) unless stated.

| Path | Content |
|---|---|
| `var/ibtrader.sqlite3` (+ `-wal`, `-shm`) | SQLite database. Tables: `raw_signals`, `signals`, `signal_status_history`, `intents`, `positions`, `pnl_events`, `producer_pauses`, `kv`, `audit_log` (append-only) |
| `var/inbox/<producer>/` | file-drop input (`*.md`) |
| `var/outbox/<producer>/<signal_id>.jsonl` | acknowledgements and lifecycle feedback for the producer |
| `var/processed/<producer>/` | accepted and duplicate documents, prefixed with a UTC timestamp |
| `var/quarantine/<producer>/` | rejected and expired documents |
| `var/logs/ibtrader.jsonl` | structured log (the same lines go to stderr) |
| `var/KILL` | kill switch flag, present means active |
| `.env`, `config/producers.yaml` | configuration. Never commit; the registry contains key hashes. |

## 16. SQL queries for the audit log

Open the database with `sqlite3 var/ibtrader.sqlite3` (add `.headers on` and `.mode column`). Timestamps
are UTC ISO 8601 text (for example `2026-10-19T14:40:05+00:00`), so comparisons such as `ts >= '2026-10-19'` work.
`data` columns hold JSON; use `json_extract`.

Recent audit events:

```sql
SELECT id, ts, event, producer, signal_id, intent_id, substr(data, 1, 120) AS data
FROM audit_log ORDER BY id DESC LIMIT 50;
```

Everything about one signal (receipt, validation, intents, orders, fills):

```sql
SELECT ts, event, intent_id, data FROM audit_log WHERE signal_id = 'nvda-momo-2026-10-19-001' ORDER BY id;
SELECT * FROM signal_status_history WHERE signal_id = 'nvda-momo-2026-10-19-001' ORDER BY id;
```

Risk rejections by rule and producer, last 7 days:

```sql
SELECT json_extract(data, '$.rule') AS rule, producer, COUNT(*) AS n
FROM audit_log WHERE event = 'risk_rejected' AND ts >= date('now', '-7 day')
GROUP BY 1, 2 ORDER BY n DESC;
```

Why intents were rejected:

```sql
SELECT id, producer, signal_id, status, rule, reason, created_at
FROM intents WHERE status = 'rejected' ORDER BY created_at DESC LIMIT 20;
```

Gateway outcomes per producer and reason (`signal_expired` appears twice with different meaning: the gateway
acknowledging an already expired document has `reason = valid_until_in_past`; a stored signal that reached its
`valid_until` has no reason):

```sql
SELECT producer, event, json_extract(data, '$.reason') AS reason, COUNT(*) AS n
FROM audit_log WHERE event IN ('signal_accepted','signal_rejected','signal_duplicate','signal_expired')
GROUP BY 1, 2, 3 ORDER BY 1, n DESC;
```

Raw payloads (verbatim, including rejected ones):

```sql
SELECT id, received_at, transport, producer, status, size, truncated FROM raw_signals ORDER BY id DESC LIMIT 20;
SELECT payload, errors FROM raw_signals WHERE id = 42;
```

Intents needing attention (match `order_ref` against TWS):

```sql
SELECT id, producer, signal_id, kind, status, reason, order_ref, updated_at
FROM intents WHERE status IN ('unknown', 'error', 'awaiting_approval') ORDER BY created_at;
```

Errors:

```sql
SELECT ts, event, producer, signal_id, data FROM audit_log
WHERE event IN ('error', 'order_error', 'order_unknown', 'exit_unpriced') ORDER BY id DESC LIMIT 50;
```

Exit retries (more than 3 exit intents for one position in a day suggests a repeating failure):

```sql
SELECT json_extract(data, '$.position_id') AS position, COUNT(*) AS exits
FROM audit_log WHERE event = 'exit_created' AND ts >= date('now', '-1 day')
GROUP BY 1 HAVING COUNT(*) > 3;
```

Kill switch and producer pause history:

```sql
SELECT ts, event, producer, data FROM audit_log
WHERE event IN ('kill_switch_activated','kill_switch_deactivated','producer_paused','producer_resumed') ORDER BY id DESC;
SELECT * FROM producer_pauses;
```

Open positions and realised P&L:

```sql
SELECT id, producer, signal_id, underlying, strategy, quantity, entry_price, shadow, opened_at
FROM positions WHERE status = 'open';
SELECT substr(at, 1, 10) AS day_utc, producer, shadow, ROUND(SUM(pnl), 2) AS pnl
FROM pnl_events GROUP BY 1, 2, 3 ORDER BY 1 DESC;
```

Rule evaluations (the indicator values used are in `data`):

```sql
SELECT ts, signal_id, json_extract(data, '$.fired') AS fired, data
FROM audit_log WHERE event = 'rule_evaluated' ORDER BY id DESC LIMIT 20;
```

Audit events written by the system, for reference: `signal_received`, `signal_accepted`, `signal_rejected`,
`signal_duplicate`, `signal_expired`, `rule_evaluated`, `risk_approved`, `risk_rejected`, `manual_approved`,
`manual_rejected`, `approval_expired`, `order_submitted`, `order_repriced`, `order_fill`, `order_filled`,
`order_partially_filled`, `order_cancelled`, `order_rejected`, `order_cancel_requested`, `order_error`,
`order_reattached`, `order_unknown`, `position_opened`, `exit_created`, `exit_unpriced`, `position_closed`,
`kill_switch_activated`, `kill_switch_deactivated`, `producer_paused`, `producer_resumed`, `error`.
