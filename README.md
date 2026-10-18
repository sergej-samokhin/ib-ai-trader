# ib-ai-trader

Receives trading signals from upstream AI agents as Markdown documents, validates them against a
published Signal Contract, translates them into US stock option orders and executes them on
Interactive Brokers (IBKR) via the TWS API. Hard risk guardrails apply to every order. **Paper
trading is the default.**

> Status: **v0.1**. The full pipeline runs end to end against simulated market data and a
> simulated broker. The IBKR adapter (`src/ibtrader/ibkr.py`) is written against `ib_async` but
> has **not yet been validated against a paper account**. Run the integration checklist below
> before you rely on it. This software is not investment advice.

## How it works

```
Upstream agents ─► Ingestion adapters ─► Signal Gateway ─► Signal Store
                  (file drop / HTTP)    (auth, validate,       │
                                         dedupe, TTL,          ▼
                                         conflicts)      Signal Evaluator ─► Contract Selector
                                                               ▲                    │
                                                        Market Data                 ▼
                                                               ▲             Risk Engine ─(reject)─► audit + notify + feedback
                                                               │                    │ approve (or manual approval)
                                                        IBKR connection ◄── Order Manager
                                                                                    │
                                                                         State store + audit log
```

| Signal kind | What happens |
|---|---|
| `trade` | One-shot. Becomes an order intent as soon as the entry window is open, then goes through the risk check. |
| `rule` | Standing strategy. A whitelisted condition such as `close > sma_20 and rsi_14 < 70` is evaluated at `check_at` or on an interval. A newer `version` replaces the old one. |
| `exit` | Closes or reduces a position opened by one of your earlier signals. Exits are always allowed, even when the kill switch is active. |

Allowed strategies: `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`. That is
debit only; naked shorts are impossible. Spreads go out as one combo (BAG) order. Orders are
limit only: they start at mid, reprice up to `max_reprice_steps` toward the natural price
(never above the risk-approved maximum), and the remainder is cancelled after that.

The contract for agent authors is in **[SIGNAL_CONTRACT.md](SIGNAL_CONTRACT.md)**, with the JSON
Schema at [`schemas/signal-contract-1.0.schema.json`](schemas/signal-contract-1.0.schema.json)
and examples in [`examples/signals/`](examples/signals/).

## Quick start (no IBKR needed)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
cp config/producers.example.yaml config/producers.yaml

pytest                                        # test suite
ibtrader validate examples/signals/valid/*.md # contract self-test
ibtrader run --demo                           # synthetic data + simulated broker, state in var/demo/
```

In a second shell, drop a signal into a producer's inbox. Edit `created_at` / `valid_until` so
the window includes the current US session; otherwise the signal is acknowledged as `expired`.

```bash
cp examples/signals/valid/trade-bull-call-spread.md var/demo/inbox/momentum-agent/
cat var/demo/outbox/momentum-agent/*.jsonl  # ack + lifecycle feedback
DATA_DIR=var/demo ibtrader status          # positions, intents, active signals
```

Outside US regular trading hours, `trade` signals stay `pending` until the entry window opens.

## Setup with IBKR

1. **Account**: options trading permission must be active (appropriateness test done) on both
   the live account and the paper account.
2. **Market data**: an API-enabled US options subscription (OPRA) and US equities. Without it,
   set `MARKET_DATA_TYPE=3` (delayed) for paper experiments. Bid/ask, greeks and open interest
   are read via `reqMktData` with generic ticks 100, 101 and 106.
3. **IB Gateway** (recommended over TWS for headless servers) with **IBC** for automated login
   and daily restarts. `docker-compose.yml` uses a community image that bundles both. Review it
   and pin a digest before use.
4. **API settings** in Gateway/TWS: enable ActiveX/Socket clients, disable *Read-Only API*, set
   the socket port (Gateway: 4002 paper / 4001 live; TWS: 7497 / 7496), and restrict trusted IPs
   to the trader host.
5. **Configure** `.env` (see [`.env.example`](.env.example)): `IB_HOST`, `IB_PORT`,
   `IB_CLIENT_ID`, `IB_ACCOUNT` (paper accounts start with `DU`), `MODE`, and the risk limits.
6. **Start**: `ibtrader run` (or `docker compose up -d`). At startup the trader refuses to run if
   `MODE` and the port or account look inconsistent (for example `MODE=paper` against an account
   that is not `DU…`).

### Integration checklist (paper account)

Before the first unattended run, go through the items in
[docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) and then run, on a paper account:

- one `long_call` and one `bull_call_spread` trade signal, through fill and an `exit` signal
- a `rule` with `check_at`, firing once
- the kill switch with a working order (`ibtrader kill on`)
- a gateway restart while an order is working, then `ibtrader reconcile`

## Configuration

All settings come from environment variables or `.env`. Signals can never change them.

| Group | Keys |
|---|---|
| Mode | `MODE` (`paper` unless exactly `live`) |
| IBKR | `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`, `IB_ACCOUNT`, `MARKET_DATA_TYPE` |
| Risk | `MAX_POSITION_USD`, `MAX_PORTFOLIO_RISK_USD`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS_USD`, `MAX_UNDERLYING_EXPOSURE_USD`, `MIN_OPEN_INTEREST`, `MAX_BID_ASK_SPREAD_PCT`, `NO_ENTRY_OPEN_MINUTES`, `NO_ENTRY_CLOSE_MINUTES`, `MANUAL_APPROVAL_ABOVE_USD`, `MIN_CONFIDENCE_AUTO`, `APPROVAL_TIMEOUT_MINUTES`, `MAX_DELTA_DEVIATION` |
| Ingestion | `FILE_DROP_ENABLED`, `HTTP_ENABLED`, `HTTP_HOST`, `HTTP_PORT`, `MAX_SIGNAL_BYTES` |
| Orders/loop | `LOOP_INTERVAL_SECONDS`, `REPRICE_INTERVAL_SECONDS`, `RULE_EVAL_INTERVAL_SECONDS`, `PRICE_TICK`, `EXIT_MAX_REPRICE_STEPS`, `DAILY_SUMMARY_TIME` |
| Notifications | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SMTP_*`, `NOTIFY_EMAIL_TO` |
| Paths | `DATA_DIR` (default `var/`), `PRODUCERS_FILE` (default `config/producers.yaml`) |

The values in `.env.example` are **placeholders**. The account owner sets the real limits.

### Risk guardrails (enforced on every entry)

Kill switch · producer paused · strategy allow-list · limit orders only · entry window (no
entries in the first/last N minutes; NYSE holidays and early closes) · daily realised loss limit
· max open positions · liquidity (open interest, bid-ask spread % of mid, fail-closed when data
is missing) · per-position cap `min(signal request, MAX_POSITION_USD)` · total premium at risk ·
per-producer budget · per-underlying exposure. Size is computed against the worst-case
(natural) price, so the caps still hold after repricing. Every rejection is audited with the
rule id and the producer.

## Producer onboarding

1. Pick an id (`^[a-z0-9][a-z0-9_-]{1,63}$`) and add it to `config/producers.yaml` with
   `status: paper_only` and a `budget_usd`.
2. **File drop** (default): create a Unix user/group for the producer that can write only to
   `var/inbox/<id>/` and read only `var/outbox/<id>/`. The directory is the identity.
   **HTTP**: run `ibtrader new-key`, give the key to the producer, store only
   `api_key_sha256` in the registry, and set `HTTP_ENABLED=true`. Put TLS in front through a
   reverse proxy if the producer runs on another host.
3. Hand the producer [SIGNAL_CONTRACT.md](SIGNAL_CONTRACT.md). They self-test with
   `ibtrader validate` or the JSON Schema.
4. Promote `paper_only → manual → auto` based on `ibtrader scorecard` (signals received,
   accepted and rejected, trades, hit rate, P&L). See [docs/RUNBOOK.md](docs/RUNBOOK.md).

| Producer status | `MODE=paper` | `MODE=live` |
|---|---|---|
| `paper_only` | trades on the paper account | **shadow only**: simulated, never sent to IBKR |
| `manual` | each entry needs `ibtrader approve <intent>` | same |
| `auto` | executes within limits | executes within limits (entries above `MANUAL_APPROVAL_ABOVE_USD` still need approval) |
| `paused` | all signals rejected | same |

## Paper → live

Follow the checklist in [docs/RUNBOOK.md](docs/RUNBOOK.md#switching-paper--live). In short: run
the integration checklist on paper, set the limits deliberately, set `MODE=live`, the live port
and `IB_ACCOUNT` explicitly, keep new producers at `paper_only`/`manual`, and watch the first
session.

## Operations

`ibtrader status | scorecard | kill on|off | producer pause|resume <id> | approve|reject <id> |
reconcile`. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for start/stop, kill switch, crash
recovery and reconciliation.

## Repository layout

```
src/ibtrader/
  contract/     Signal Contract models, parser, JSON Schema export
  ingestion/    file-drop and HTTP adapters (transport only)
  gateway.py    auth, validation, dedupe, TTL, versioning, conflict policy
  evaluator.py  rule evaluation        expr.py / indicators.py  condition language
  selector.py   expiry/strike/right    risk.py   risk engine (single veto)
  orders.py     order manager          monitor.py  TP/SL/time exits
  app.py        Trader orchestration   store.py  SQLite state + audit log
  ibkr.py       ib_async adapters      sim.py    synthetic market data + simulated broker
  cli.py        `ibtrader` command
docs/           ADR, runbook, assumptions
examples/       valid / invalid / adversarial signals
schemas/        generated JSON Schema (CI checks it is current)
```

## Development

```bash
make install   # pip install -e ".[dev]"
make lint      # ruff check + format check
make test      # pytest
make schema    # regenerate the JSON Schema after changing contract models
```

CI (`.github/workflows/ci.yml`) runs lint, tests, checks that the schema is up to date, and
validates the example signals.

## Further reading

- [SIGNAL_CONTRACT.md](SIGNAL_CONTRACT.md): for upstream agent authors
- [docs/adr/0001-signal-ingestion.md](docs/adr/0001-signal-ingestion.md): transport decision
- [docs/RUNBOOK.md](docs/RUNBOOK.md): operations
- [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md): v0.1 decisions, assumptions to verify, limitations
- [CHANGELOG.md](CHANGELOG.md)
