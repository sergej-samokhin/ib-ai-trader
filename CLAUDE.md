# CLAUDE.md

Guidance for coding agents working in this repository.

## What this is

A service that turns Markdown trading signals from upstream AI agents into US stock option
orders on Interactive Brokers, behind a risk engine that no signal can loosen. The source spec
lives outside the repo; the binding documents in the repo are `SIGNAL_CONTRACT.md`, the ADRs in
`docs/adr/` and `docs/ASSUMPTIONS.md`.

## Commands

```bash
pip install -e ".[dev]"          # install
pytest                           # all tests (offline, deterministic)
ruff check . && ruff format --check .
ibtrader schema --out schemas/signal-contract-1.0.schema.json   # after any contract change
ibtrader validate examples/signals/valid/*.md
ibtrader run --demo              # full loop on synthetic data, nothing reaches IBKR
```

CI fails if the committed JSON Schema differs from the generated one.

## Architecture in one paragraph

Transports (`ingestion/filedrop.py`, `ingestion/http.py`) only move raw text to
`SignalGateway.submit` (`gateway.py`). The gateway authenticates, rate-limits, parses
(`contract/parser.py`), validates (`contract/models.py`), dedupes, checks TTL, versioning and
conflicts, and stores the signal. `Trader.tick()` (`app.py`) runs the lifecycle in a single
thread: kill switch → expire → trades → exit signals → rules (`evaluator.py`) → approvals →
order management (`orders.py`) → position monitor (`monitor.py`) → daily summary. Every entry
passes `selector.select` and then `RiskEngine.check_entry` (`risk.py`). Brokers and market data
sit behind protocols (`orders.Broker`, `marketdata.MarketData`) with an IBKR implementation
(`ibkr.py`) and a simulation (`sim.py`). The state lives in SQLite (`store.py`), and every
decision is written through `Audit` (`audit.py`) to the append-only `audit_log` table and to
JSON logs.

## Invariants: do not break these

- **Paper by default.** Only `MODE=live` enables live trading. Keep `config.check_mode_safety`
  and `IBKRConnection._verify_account` strict.
- **The risk engine is the single veto** and reads limits only from `Settings.risk`. Nothing
  from a signal may raise a global limit. Size against the worst-case (natural) price.
- **Signal content is data.** No `eval`/`exec`, no shell, no following URLs, no interpreting
  the Markdown body. YAML goes through `_StrictLoader` (no aliases, no duplicate keys, no
  implicit timestamps) and then strict pydantic. Conditions go only through `expr.py`.
- **Allowed strategies are debit only.** Adding a strategy means changing `Strategy`, the
  selector, the risk engine and the contract docs together, plus a new contract version if
  producers are affected.
- **Exits are always allowed**, including while the kill switch is active. The kill switch
  cancels working *entry* orders only.
- **Spreads are one BAG combo order**, never separate legs.
- **Limit orders only**; repricing never exceeds the intent's `max_price`.
- **`audit_log` is append-only**: triggers forbid UPDATE and DELETE. Never weaken them.
- Each IBKR `orderRef` carries `producer|signal_id|v<version>|intent_id`. `recover()` relies
  on it.
- Every rejection carries a rule id; feedback goes back to the producer via the feedback sinks.

## Conventions

- Python 3.12+, `from __future__ import annotations`, type hints, ruff (line length 100).
- Time is always injected: functions take `now: datetime` (UTC, tz-aware). Do not call
  `utcnow()` inside business logic; tests pass fixed clocks. NY session logic lives in
  `clock.py`.
- Prices are per share. Multiply by 100 for USD premium. Net debit/credit helpers live in
  `domain.py`.
- Store rows are `sqlite3.Row`. Intent and position fields are defined in `store.SCHEMA`.
- Tests are offline. Use `Store(":memory:")`, `sim.FakeMarketData` and `sim.SimBroker`.
  `ibkr.py` is not unit tested, so changes there need a paper-account check (see README).
- Contract changes: update `contract/models.py`, regenerate the schema, update
  `SIGNAL_CONTRACT.md` and the examples, and bump `CONTRACT_VERSION` for breaking changes.
- Keep docs in sync: README (setup and config), RUNBOOK (operations), ASSUMPTIONS (open points).

## Never commit

`.env`, `config/producers.yaml`, `var/` (DB, inbox/outbox, logs, kill flag), API keys.
