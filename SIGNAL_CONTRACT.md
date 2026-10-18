# Signal Contract 1.0

`contract_version: "1.0"`

This document is the interface specification for upstream agents (and the people who build them)
that send trading signals to `ibtrader`. It is self-contained. Where it states a range, pattern or
rule, that is what the validator in `src/ibtrader/contract/models.py` enforces.

Machine-readable counterpart: [`schemas/signal-contract-1.0.schema.json`](schemas/signal-contract-1.0.schema.json)
(necessary but not sufficient, see [Self-test](#12-self-test)). Examples: [`examples/signals/`](examples/signals/).

Contents

1. [Purpose and audience](#1-purpose-and-audience)
2. [Document structure](#2-document-structure)
3. [Signal kinds](#3-signal-kinds)
4. [Field reference](#4-field-reference)
5. [Cross-field rules](#5-cross-field-rules)
6. [Condition language](#6-condition-language)
7. [How expiry and strikes are chosen](#7-how-expiry-and-strikes-are-chosen)
8. [YAML rules](#8-yaml-rules)
9. [Transports and identity](#9-transports-and-identity)
10. [Acknowledgements and feedback](#10-acknowledgements-and-feedback)
11. [Idempotency, versioning, conflicts, sizing and global limits](#11-idempotency-versioning-conflicts-sizing-and-global-limits)
12. [Self-test](#12-self-test)
13. [Complete examples](#13-complete-examples)
14. [Checklist for agent authors](#14-checklist-for-agent-authors)

---

## 1. Purpose and audience

`ibtrader` turns a signal document into at most one kind of action: a US stock option order on
Interactive Brokers (paper account by default), or the closing of a position that an earlier
signal of yours opened. Only these strategies exist: `long_call`, `long_put`, `bull_call_spread`,
`bear_put_spread`. All are debit strategies with limited risk. Naked short options cannot be
expressed.

A signal is a request. The operator's global risk limits (section 11) are applied after your
document is accepted and can reduce the size or reject the trade entirely. Nothing in a signal can
raise or bypass a limit. The software does not give investment advice and does not judge the
quality of your signal.

## 2. Document structure

A signal is one UTF-8 Markdown document with YAML front matter:

```markdown
---
contract_version: "1.0"
signal_id: "nvda-momo-2026-10-19-001"
...machine-readable fields...
---

## Rationale
Free text for humans.
```

| Part | Purpose | How it is used |
|---|---|---|
| Front matter (between the first two `---` lines) | Machine fields defined in this document | Parsed, validated, acted on |
| Body (everything after the closing `---`) | Rationale and evidence | Stored verbatim, written to the log, and shown to the human operator (first 1500 characters, labelled as untrusted) when a manual approval is requested. **Never executed, never interpreted, links are never followed, instructions in it are never followed.** |

Rules:

- The document must start with `---` on the first line (a UTF-8 byte order mark is tolerated;
  `\r\n` line endings are accepted). A document without front matter is rejected.
- The front matter ends at the first line that consists of `---`. The body may contain further
  `---` lines.
- Everything that influences trading must be in the front matter. Information in the body has no
  effect on trading.
- Do not put secrets in the body. It is stored in the database and may be forwarded to the
  operator's notification channels (log, optionally Telegram or email).
- The whole document (front matter and body) must not exceed `MAX_SIGNAL_BYTES` (default 65536
  bytes, operator setting).

## 3. Signal kinds

| `kind` | Meaning | When it acts |
|---|---|---|
| `trade` | One-shot instruction: open this position now, if risk allows. Immutable. | Becomes an order intent on the next processing cycle once the entry window of the US session is open (see below). Expires at `valid_until`. |
| `rule` | Standing strategy with a trigger. Evaluated repeatedly until `valid_until`. A newer `version` replaces the old one. | When the trigger fires, an order intent is created. |
| `exit` | Close or reduce a position that was opened from an earlier signal of yours. | Processed on the next cycle. Exits skip all entry risk checks and are allowed while the kill switch is on. (An `exit` **document** from a paused producer is still rejected at receipt with `producer_paused`; the stop-loss, take-profit and time exits of existing positions keep running.) |

Behaviour details that affect how you write signals:

- **Entry window.** New entries are only possible during regular US equity hours (America/New_York,
  09:30 to 16:00, 13:00 on early-close days), excluding the first `NO_ENTRY_OPEN_MINUTES` and the
  last `NO_ENTRY_CLOSE_MINUTES` (operator settings, both 15 in `.env.example`; the end of the
  window is exclusive). With the example values the window is 09:45 to 15:45 (12:45 on early-close
  days), so the latest effective `check_at` is `15:44`. Weekends and NYSE holidays
  (NYSE calendar from `pandas_market_calendars`) are closed.
- **`trade` outside the window.** The signal stays `pending` until the window opens. If
  `valid_until` passes first, it expires (feedback event `expired`). Set `valid_until` so that it
  extends into the entry window you are aiming for.
- **A `trade` is consumed on its first attempt.** Once the intent has been created, the signal is
  `consumed`, whatever the risk engine decides. A rejected trade is not retried. Send a new
  `signal_id` if you want another attempt. This includes rejections by the kill switch.
- **`rule` with `trigger.type: condition`.**
  - With `check_at`: evaluated once per trading day, on the first processing cycle at or after that
    time, even if that cycle falls outside the entry window (the entry is then rejected, see below).
  - Without `check_at`: evaluated every `RULE_EVAL_INTERVAL_SECONDS` (default 300) while the entry
    window is open.
- **`rule` with `trigger.type: schedule`.** Fires unconditionally, once per trading day, on the
  first processing cycle at or after `check_at`.
- **`max_entries_per_day` counts firings, not fills.** Each time a rule fires it uses one of the
  day's entries (New York date), even if the risk engine then rejects the entry or the order is never
  filled. A rule whose `check_at` lies outside the entry window therefore fires, is rejected with
  rule `market_hours`, and has used its entry for that day.
- **A rule does not know whether it already holds a position.** If the condition stays true it can
  enter again, up to `max_entries_per_day` per day, on every trading day until `valid_until`. Design
  the condition accordingly.
- **An `exit` does not retire a rule.** It closes or reduces positions and cancels not-yet-filled
  entry orders for `target_signal_id`, but a targeted `rule` stays active and can fire again. To
  stop a rule, replace it with a newer `version` whose `valid_until` is only slightly after
  `created_at`, or ask the operator to pause your producer id.
- **Exit rules are fixed at fill time.** A position keeps the `exit` block (take profit, stop loss,
  close before expiry) of the signal version that opened it. A newer rule version does not change
  open positions.

## 4. Field reference

Unless noted, all keys are required for the kinds listed and **no other keys are allowed**
(unknown keys are rejected). Types are strict: `"21"` is not an integer, `2.0` is not an integer,
`true` is not a number. A YAML integer is accepted where a float is expected (`max_premium_usd: 300`).

### 4.1 Envelope (all kinds)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `contract_version` | string | yes | exactly `"1.0"` |
| `signal_id` | string | yes | pattern `^[a-z0-9][a-z0-9._-]{2,127}$`; unique per producer |
| `version` | integer | yes | 1 to 1000000. `trade` and `exit` must be `1`. For `rule`, increase it on every change. |
| `kind` | string | yes | `rule`, `trade` or `exit` |
| `producer` | string | yes | pattern `^[a-z0-9][a-z0-9_-]{1,63}$`; must equal the authenticated identity |
| `created_at` | timestamp | yes | ISO 8601 **with** timezone (`Z` or offset). Not later than receipt time plus `MAX_CLOCK_SKEW_SECONDS` (default 300 s). |
| `valid_until` | timestamp | yes | ISO 8601 with timezone; must be later than `created_at`. At or before receipt time the signal is acknowledged `expired`. |
| `confidence` | number | yes | 0 to 1 inclusive. Required for all kinds including `exit`. |

`confidence` is informational. It is not used for sizing. It has one effect: if the operator sets
`MIN_CONFIDENCE_AUTO` above 0, entries with a lower `confidence` require manual approval.

### 4.2 Underlying and strategy (`trade`, `rule`)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `underlying` | string | yes | US ticker, pattern `^[A-Z]{1,5}(\.[A-Z])?$` (for example `NVDA`, `BRK.B`). Quote it (see section 8). |
| `strategy` | string | yes | `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread` |

Direction (used for conflict detection): `long_call` and `bull_call_spread` are bullish;
`long_put` and `bear_put_spread` are bearish.

### 4.3 `contract` block (`trade`, `rule`)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `contract.dte_min` | integer | yes | 0 to 365 (calendar days to expiry) |
| `contract.dte_max` | integer | yes | 0 to 365; `dte_min` must be less than or equal to `dte_max` |
| `contract.long_leg_delta` | number | yes | 0.05 to 0.95 (absolute delta; use positive numbers for puts too) |
| `contract.short_leg_delta` | number | spreads only | 0.01 to 0.95; **required** for `bull_call_spread` and `bear_put_spread`, **not allowed** for `long_call` and `long_put` |

### 4.4 `order` block (`trade`, `rule`)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `order.type` | string | yes | exactly `limit` (no market orders) |
| `order.price` | string | yes | exactly `mid` (the order starts at the net mid price) |
| `order.max_reprice_steps` | integer | yes | 0 to 10 |

Spreads are always sent as one combo order, never as separate legs. After submission the order is
repriced every `REPRICE_INTERVAL_SECONDS` (default 20 s) in `max_reprice_steps` equal steps from the
mid toward the natural price (pay ask, receive bid), never above the maximum price the risk engine
approved. When the steps are used up, the remainder is cancelled. With `max_reprice_steps: 0` the
order is cancelled at the first repricing check (after about `REPRICE_INTERVAL_SECONDS`) if it has not
filled.

### 4.5 `sizing` block (`trade`, `rule`)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `sizing.max_premium_usd` | number | yes | greater than 0, at most 100000. Total premium for the whole position (all contracts), in USD. |
| `sizing.max_contracts` | integer | yes | 1 to 100 |

Both are upper bounds on your request. See section 11 for how the global limits cut them down.

### 4.6 `exit` block (`trade`, `rule`)

Exit rules attached to the position that the entry creates. They are checked by the position monitor
during regular session hours.

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `exit.take_profit_pct` | number | yes | greater than 0, at most 1000. Gain in percent of the entry price per share (mid-based). |
| `exit.stop_loss_pct` | number | yes | greater than 0, at most 100. Loss in percent of the entry price. |
| `exit.close_before_expiry_days` | integer | yes | 0 to 60. Closes the position when the days to the nearest expiry are less than or equal to this value. |

### 4.7 Trigger (`rule` only)

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `trigger.type` | string | yes | `condition` or `schedule` |
| `trigger.condition` | string | for `condition` | at most 500 characters, [condition language](#6-condition-language). **Not allowed** for `schedule`. |
| `trigger.check_at` | string | for `schedule`; optional for `condition` | pattern `^([01]\d|2[0-3]):[0-5]\d America/New_York$`, for example `"15:30 America/New_York"`. Only the zone `America/New_York` is accepted. |
| `max_entries_per_day` | integer | yes (`rule` only) | 1 to 10 |

### 4.8 Exit-kind fields (`exit` only)

An `exit` signal has the envelope fields (section 4.1) plus exactly these:

| Field | Type | Required | Allowed values / range |
|---|---|---|---|
| `target_signal_id` | string | yes | same pattern as `signal_id`. Must be a `trade` or `rule` signal that **you** sent and that was accepted. |
| `action` | string | yes | `close` or `reduce` |
| `reduce_pct` | integer | no (default 100) | 1 to 100. See section 5. |

An exit signal has no `underlying`, `strategy`, `contract`, `order`, `sizing`, `exit` or `trigger`
keys (they would be rejected as unknown). It acts on all open positions and not-yet-filled entry
orders that came from `target_signal_id` (any version). Close: sells the whole position. Reduce: sells
`max(1, floor(quantity * reduce_pct / 100))` contracts of each matching position.

## 5. Cross-field rules

These cannot be expressed in the JSON Schema. The validator rejects the document with
`validation_failed`; the field name is `$` unless stated and the message names the offending field.

| Rule | Error message (`message`) |
|---|---|
| `valid_until` must be later than `created_at` | `valid_until must be after created_at` |
| `contract.dte_min` less than or equal to `contract.dte_max` (field `contract`) | `dte_min must be <= dte_max` |
| Spreads require `contract.short_leg_delta` | `contract.short_leg_delta is required for <strategy>` |
| `contract.short_leg_delta` must be smaller than `contract.long_leg_delta` (short leg is further out of the money) | `contract.short_leg_delta must be smaller than long_leg_delta` |
| Single-leg strategies must not carry `contract.short_leg_delta` | `contract.short_leg_delta is not allowed for <strategy>` |
| `exit.close_before_expiry_days` must be smaller than `contract.dte_min` (not checked when `dte_min` is 0) | `exit.close_before_expiry_days must be smaller than contract.dte_min` |
| `trade` signals must have `version: 1` (field `version`) | `trade signals are immutable; version must be 1` |
| `exit` signals must have `version: 1` (field `version`) | `exit signals are immutable; version must be 1` |
| `action: close` requires `reduce_pct` to be 100 or omitted | `reduce_pct must be 100 (or omitted) for action 'close'` |
| `action: reduce` requires `reduce_pct` of 1 to 99 (100 is rejected, use `close`) | `use action 'close' to exit 100%` |
| `trigger.type: condition` requires `trigger.condition` (field `trigger`) | `trigger.condition is required for type 'condition'` |
| `trigger.type: schedule` requires `trigger.check_at` and forbids `trigger.condition` (field `trigger`) | `trigger.check_at is required for type 'schedule'`, `trigger.condition is not allowed for type 'schedule'` |
| `trigger.condition` must parse in the condition language (field `trigger`) | `trigger.condition: <parser message>` |

## 6. Condition language

Used in `trigger.condition`. It is a small whitelisted expression language implemented in
`src/ibtrader/expr.py`. It has no function calls, attribute access, strings, variables or anything
executable. Anything outside the grammar is rejected when the signal is received.

### Grammar

```
expr       := or_expr
or_expr    := and_expr ("or" and_expr)*
and_expr   := not_expr ("and" not_expr)*
not_expr   := "not" not_expr | comparison
comparison := arith (CMP arith)?
arith      := term (("+" | "-") term)*
term       := factor (("*" | "/") factor)*
factor     := NUMBER | INDICATOR | "(" expr ")" | "-" factor
CMP        := "<" | "<=" | ">" | ">=" | "==" | "!="
NUMBER     := digits, optionally "." digits      (no sign, no exponent, no leading ".")
```

- Keywords and indicator names are lower case. `SMA_20` or `AND` are rejected.
- The whole expression must be boolean. `and`, `or`, `not` take comparisons; comparisons and
  arithmetic take numbers. `close`, `not close`, `close > 1 and 2` and `(close > 1) == (open > 1)` are
  rejected.
- At most one comparison operator per comparison (`1 < close < 2` is rejected; write `1 < close and close < 2`).
- Limits: at most 500 characters, 200 tokens, nesting depth 20.

### Indicators

All are computed from daily bars of the underlying (regular trading hours). The latest bar is the
current trading day's bar, so during the session `close` is the most recent price of the day, not
the previous close.

| Name | Meaning |
|---|---|
| `close`, `open`, `high`, `low`, `volume` | Latest daily bar |
| `prev_close` | Close of the previous daily bar |
| `sma_N` | Simple moving average of the last N closes |
| `ema_N` | Exponential moving average of closes (seed: SMA of the first N closes, smoothing 2/(N+1)) |
| `rsi_N` | Wilder RSI of closes |
| `avg_volume_N` | Simple average of the last N daily volumes |

`N` is an integer from **2 to 200** (`sma_1` and `sma_201` are rejected; one to three digits are
parsed, so avoid leading zeros). If there is not enough history for an indicator, or a division by
zero occurs, the condition does not fire and the evaluation is logged with an `error` entry.

### Valid examples

```
close > sma_20 and rsi_14 < 70
volume > 1.5 * avg_volume_20
close > prev_close * 1.02 or close < prev_close * 0.98
ema_10 > ema_50 and close > open
not (rsi_14 > 70)
(close + open) / 2 > sma_20
close > sma_20 and (rsi_14 < 70 or rsi_14 > 80)
```

### Invalid examples

| Expression | Why |
|---|---|
| `close` | not boolean ("condition must evaluate to true/false") |
| `close > 1 > 0` | chained comparison |
| `close > 1 and 2` | `and` requires comparisons on both sides |
| `not close` | `not` applied to a number |
| `sma_1 > 1`, `sma_201 > 1` | period outside 2 to 200 |
| `sma(20) > close` | function call syntax; unknown indicator `sma` |
| `SMA_20 > 1` | upper case |
| `close = 1` | `=` is not an operator, use `==` |
| `close > 1 && rsi_14 < 70` | `&&` is not an operator, use `and` |
| `close > 1e3`, `close > .5`, `close > 1_000` | number format not supported |
| `close > 'a'`, `"x" == "x"` | strings do not exist |
| `close.real > 1` | attribute access does not exist |
| `__import__('os').system('id')` | rejected at the first character (see `examples/signals/invalid/code-injection-condition.md`) |

## 7. How expiry and strikes are chosen

Done by the contract selector (`src/ibtrader/selector.py`) at the moment the intent is created,
from live quotes. You cannot name an expiry or a strike; you describe a window and delta targets.

1. **Expiry.** Among the listed expiries whose calendar days to expiry (New York date) lie within
   `[dte_min, dte_max]`, the one **closest to the midpoint** `(dte_min + dte_max) / 2` is chosen. On a
   tie the earlier expiry wins. No expiry in the window: rejection with rule `contract_selection`.
2. **Long leg.** Among quotes of the right type (call for `long_call` and `bull_call_spread`, put
   for `long_put` and `bear_put_spread`) that have a delta and a two-sided market, the strike whose
   absolute delta is **nearest to `long_leg_delta`** is chosen (tie: lower strike). If the nearest
   delta is more than `MAX_DELTA_DEVIATION` away (operator setting, 0.10 in `.env.example`), the
   selection fails (rule `contract_selection`).
3. **Short leg (spreads).** Candidates are only strikes **further out of the money** than the long
   leg (higher strike for calls, lower strike for puts). The one with absolute delta nearest to
   `short_leg_delta` is chosen, with the same tolerance.
4. **Debit check.** The position must price as a net debit at mid, otherwise the selection fails.
5. **Liquidity is checked after the selection.** The selector does not skip an illiquid strike to find
   a better one. If the chosen contract fails the open-interest or bid-ask filter, the entry is
   rejected (`liquidity_open_interest`, `liquidity_spread`).

Practical consequences: use targets that exist in the listed chain. The IBKR adapter only quotes the
48 strikes closest to the current price, so extreme delta targets on underlyings with narrow strike
spacing can fail with `contract_selection`. Each spread leg is one contract per spread; the quantity
comes from the risk engine.

## 8. YAML rules

The front matter is parsed with a restricted YAML loader.

- **No anchors or aliases** (`&id`, `*id`). Rejected with `invalid YAML: YAML aliases/anchors are not allowed`.
- **No duplicate keys.** Rejected with `invalid YAML: duplicate key '<key>'`.
- **No custom tags** (for example `!!python/object`). Rejected as invalid YAML.
- **One document**, and the top level must be a mapping.
- `NaN` and infinity are rejected.
- **Timestamps are read as strings and validated by the contract.** Plain `2026-10-18T13:40:00Z`
  works, but **quote timestamps** (`"2026-10-18T13:40:00Z"`) so that YAML libraries and the JSON
  Schema see strings. A timestamp without timezone is rejected (`Input should have timezone info`).
- **Quote strings that YAML could read as another type.** In YAML 1.1 the unquoted words `ON`, `OFF`,
  `YES`, `NO` and `TRUE`/`FALSE` are booleans. `underlying: ON` is rejected with
  `Input should be a valid string`; write `underlying: "ON"`. Quoting `signal_id`, `producer`,
  `underlying`, `condition` and `check_at` is recommended.
- Numbers must be real numbers: write `0.40`, not `"0.40"`.
- Size limit: `MAX_SIGNAL_BYTES` (default 65536 bytes) for the entire document. Larger payloads are
  rejected with `payload_too_large`; the stored copy is truncated to the limit.

## 9. Transports and identity

Two transports exist; both hand the raw document to the same gateway, which applies identical
rules. Your **identity is proven by the transport, not by the document**. The `producer` field must
equal that identity or the document is rejected with `producer_mismatch`. Producers must be
registered by the operator (`config/producers.yaml`); unknown identities are rejected with
`unknown_producer`. See [`docs/adr/0001-signal-ingestion.md`](docs/adr/0001-signal-ingestion.md) for the design.

### 9.1 File drop (default)

- Write the document to `<DATA_DIR>/inbox/<producer>/<name>.md` (`DATA_DIR` is `var/` by default).
  The directory is your identity; the operator restricts it with OS permissions (one Unix user per producer).
- Only regular `*.md` files directly in that directory are read. Symbolic links and other extensions
  are ignored. Files modified less than 1 second ago are skipped as "still being written". To be safe,
  write under a different name (for example `name.tmp`) and rename to `name.md` when complete.
- The gateway polls every `LOOP_INTERVAL_SECONDS` (default 5 s). Afterwards the file is moved:
  accepted or duplicate to `<DATA_DIR>/processed/<producer>/`, rejected or expired to
  `<DATA_DIR>/quarantine/<producer>/`, prefixed with a UTC timestamp.
- Acknowledgement and lifecycle feedback are appended as JSON lines to
  `<DATA_DIR>/outbox/<producer>/<signal_id>.jsonl`. If no `signal_id` could be read from the document,
  the file is named after the input file (`<name>.jsonl`). Characters other than letters, digits
  `.`, `_` and `-` are dropped from the file name.
- No acknowledgement is written for a directory that does not belong to a registered producer.

### 9.2 HTTP (optional, enabled by the operator)

| Request | Purpose |
|---|---|
| `POST /v1/signals` | Body: the Markdown document (the `Content-Length` header is used; `Content-Type` is not checked). Header `Authorization: Bearer <api key>`. |
| `GET /v1/signals/<signal_id>` | Status history and order intents of one of **your** signals. Same bearer header. |
| `GET /healthz` | `{"ok": true}`, no authentication |

```bash
curl -sS -X POST "$IBTRADER_URL/v1/signals" \
  -H "Authorization: Bearer $IBTRADER_API_KEY" \
  --data-binary @signal.md
```

| HTTP code | Meaning |
|---|---|
| `202` | `accepted` |
| `200` | `duplicate` |
| `422` | `rejected` or `expired` (reason and `errors` in the JSON body) |
| `429` | `rate_limited` |
| `401` | missing or unknown API key (not stored, not acknowledged by the gateway) |
| `411` | `Content-Length` header is not a valid number |
| `404` | unknown path, or `GET` for a `signal_id` that has no history |

The response body of `POST` is the acknowledgement object of section 10. The adapter speaks plain
HTTP; the operator runs it on localhost or behind a TLS-terminating reverse proxy. Keep the API key
secret; the operator stores only its SHA-256 hash.

`GET /v1/signals/<signal_id>` returns:

```json
{
  "signal_id": "nvda-momo-2026-10-19-001",
  "history": [
    {"version": 1, "status": "pending", "reason": "accepted", "at": "2026-10-19T14:40:05+00:00"},
    {"version": 1, "status": "consumed", "reason": "intent created", "at": "2026-10-19T14:40:10+00:00"}
  ],
  "intents": [
    {"id": "int_4bdff870d745", "kind": "entry", "status": "filled", "rule": null, "reason": "approved",
     "quantity": 1, "filled_qty": 1, "avg_fill_price": 2.1, "updated_at": "2026-10-19T14:41:00+00:00"}
  ]
}
```

Signal `status` values in `history`: `pending` (trade or exit waiting), `active` (rule), `consumed`,
`no_position` (exit that found nothing to act on), `expired`, `superseded`. Intent `kind` is `entry`
or `exit`. Intent `status` values: `awaiting_approval`, `approved`, `submitted`, `filled`,
`partially_filled`, `cancelled`, `rejected`, `expired`, `error`, `unknown`.

## 10. Acknowledgements and feedback

### 10.1 Acknowledgement (immediate)

Every submitted document gets exactly one acknowledgement. Over HTTP it is the response body; for
file drop it is the first line written to the outbox file (`"event": "ack"`).

```json
{"status": "rejected", "signal_id": null, "version": null, "reason": "validation_failed",
 "errors": [{"field": "order.type", "message": "Input should be 'limit'"}]}
```

| Field | Meaning |
|---|---|
| `status` | `accepted`, `rejected`, `duplicate` or `expired` |
| `signal_id`, `version` | Echoed when the document could be parsed far enough; `null` for `validation_failed` and for failures before parsing |
| `reason` | Reason code, or `null` for `accepted` |
| `errors` | List of `{"field": ..., "message": ...}`; empty unless the rejection is about specific fields |

`accepted` means the document passed the contract and the gateway checks. It does **not** mean an
order was placed. File-drop lines also carry `at` (UTC timestamp) and `file` (input file name).

**Field-level error format.** `field` is a dotted path into the front matter (`contract.dte_min`,
`sizing.leverage`, `trigger.check_at`). `$` means the document as a whole, used for structure errors
(no front matter, invalid YAML) and for cross-field rules that name the fields in the message.
`message` is a short English text; unknown keys give `unknown key`, missing keys give
`required key missing`. All problems found in one pass are listed. Examples:

```json
{"field": "contract.dte_min", "message": "Input should be a valid integer"}
{"field": "sizing.leverage", "message": "unknown key"}
{"field": "valid_until", "message": "required key missing"}
{"field": "$", "message": "contract.short_leg_delta must be smaller than long_leg_delta"}
{"field": "$", "message": "invalid YAML: YAML aliases/anchors are not allowed"}
```

**Reason codes** (checks run in this order; the first failing check decides):

| Order | `status` | `reason` | Meaning |
|---|---|---|---|
| 1 | rejected | `payload_too_large` | document larger than `MAX_SIGNAL_BYTES` |
| 2 | rejected | `unknown_producer` | identity is not in the producer registry |
| 3 | rejected | `rate_limited` | more than `rate_limit_per_hour` documents (any outcome, including this one) from your identity in the last 60 minutes. HTTP 429. |
| 4 | rejected | `producer_paused` | operator paused your producer id |
| 5 | rejected | `validation_failed` | contract violation; see `errors` |
| 6 | rejected | `producer_mismatch` | `producer` differs from the authenticated identity (field `producer`) |
| 7 | rejected | `created_in_future` | `created_at` later than receipt time plus `MAX_CLOCK_SKEW_SECONDS` (field `created_at`) |
| 8 | duplicate | `already_processed` | this `signal_id` and `version` was accepted before |
| 9 | expired | `valid_until_in_past` | `valid_until` is at or before the receipt time |
| 10 | rejected | `signal_id_reused` | the `signal_id` was used before for another kind (field `signal_id`) |
| 10 | rejected | `immutable` | reserved for a repeated `trade` or `exit` id with another version. Because both must carry `version: 1`, such a document normally ends as `validation_failed` or `duplicate` instead. |
| 10 | rejected | `stale_version` | a `rule` with a lower `version` than the latest accepted one (field `version`) |
| 11 | rejected | `unknown_target` | `exit` whose `target_signal_id` is not an accepted `trade` or `rule` signal of yours (field `target_signal_id`) |
| 11 | rejected | `conflict` | conflict policy, see section 11 (field `underlying`) |

Rejected and expired documents are **not** stored as signals (the raw text is kept for audit), so you
may correct the document and send it again with the same `signal_id` and `version`. Accepted
documents are final for that `signal_id` and `version`.

### 10.2 Lifecycle feedback (later)

After acceptance, events are appended to the same outbox file (file drop). Each line is a JSON
object with `at` (UTC timestamp), `signal_id`, `version` and `event`. Over HTTP there is no push; poll
`GET /v1/signals/<signal_id>`. (The same lines are also written to the outbox on the operator's
host when file drop is enabled.)

| `event` | When | Extra fields |
|---|---|---|
| `approved` | The risk engine approved the entry; the order will be submitted. | `intent_id`, `quantity` |
| `awaiting_approval` | Risk approved, but a human must approve first (see section 11). | `intent_id`, `quantity` |
| `rejected` | The entry was rejected. | `rule`, `reason`, `intent_id` |
| `order_filled` | An order (entry or exit; no field says which, use `GET` for `kind`) filled completely. | `intent_id`, `filled_qty` |
| `order_partially_filled` | Order ended with a partial fill (rest cancelled). | `intent_id`, `filled_qty` |
| `order_cancelled` | Order ended with no fill (for example repricing steps used up, kill switch). | `intent_id`, `filled_qty` |
| `order_rejected` | The broker rejected or deactivated the order. | `intent_id`, `filled_qty` |
| `expired` | A pending trade, exit or active rule reached `valid_until`. | none |
| `consumed` | An `exit` was processed and acted on at least once. | `actions` |
| `no_position` | An `exit` was processed but found no position or open entry order to act on. | `actions` (0) |

`rule` values in `rejected` events are the risk rule ids of section 11, plus:

| `rule` | Meaning |
|---|---|
| `contract_selection` | No expiry or strike satisfied the `contract` block (section 7) |
| `market_data` | A market-data error occurred while the contract was being selected (feed outage); the entry is rejected, not retried |
| `unknown_producer` | Producer was removed from the registry after acceptance |
| `price_moved` | Right before submission the mid was above the approved maximum price |
| `market_hours` | Entry window not open at creation or at submission |
| `kill_switch` | Kill switch active |
| `manual` | A human rejected the approval request (no `reason` field in the event) |

Events that are **not** sent to producers: manual approval, approval timeout, order submission
errors, position opened, take-profit/stop-loss/time exits as such (you only see the resulting
`order_*` event of the exit order), and a restart that could not find an order. Use `GET` for the intent
status (`expired`, `error`, `unknown`).

Example outbox lines:

```json
{"at": "2026-10-19T14:40:05.123456+00:00", "event": "ack", "file": "trade.md", "status": "accepted", "signal_id": "nvda-momo-2026-10-19-001", "version": 1, "reason": null, "errors": []}
{"at": "2026-10-19T14:40:10.200000+00:00", "signal_id": "nvda-momo-2026-10-19-001", "version": 1, "event": "approved", "intent_id": "int_4bdff870d745", "quantity": 1}
{"at": "2026-10-19T14:41:00.300000+00:00", "signal_id": "nvda-momo-2026-10-19-001", "version": 1, "event": "order_filled", "intent_id": "int_4bdff870d745", "filled_qty": 1}
```

## 11. Idempotency, versioning, conflicts, sizing and global limits

### Idempotency

The pair `(producer, signal_id, version)` is processed at most once. A second document with the same
pair is acknowledged `duplicate` (`already_processed`) and ignored, even after the signal has expired.
The `signal_id` must be unique per producer and must keep its kind: reuse with another kind is
`signal_id_reused`.

### Versioning

- `trade` and `exit` are immutable and carry `version: 1`.
- A `rule` is replaced by sending the same `signal_id` with a higher `version` (versions need not be
  consecutive). The previous version becomes `superseded` as soon as the new one is accepted. Open
  positions, and entry orders that already exist, of the old version are not touched.
- A `rule` with a version lower than the latest accepted one is rejected with `stale_version`; the
  same version again is a `duplicate`.
- The `contract_version` is `"1.0"`. The gateway accepts no other value. Changes that would break
  existing documents will use a new contract version.

### Conflict policy

If your new `trade` or `rule` has the **opposite direction** on the same underlying as an active
signal (`pending` or `active`) or an open position of a **different producer**, the newer signal is
rejected with `conflict`, and the operator is notified. Opposite directions of the same producer are not
a conflict. The error message names the other producer and signal. Conflicts are only checked when a
signal is received; `exit` signals are never subject to it.

### Sizing is a request; global limits win

The risk engine checks every entry against settings that only the operator can change. A signal
can ask for less, never for more. Checks run in this order and the first failure rejects the entry
with the rule id shown (the id appears in the `rejected` feedback event and in the audit log):

| Rule id | Check | Operator setting (`.env`) |
|---|---|---|
| `kill_switch` | No entries while the kill switch is on | `var/KILL` file or `ibtrader kill on` |
| `producer_paused` | Producer not paused | `ibtrader producer pause`, registry `status: paused` |
| `strategy_not_allowed` | Strategy in the allow-list | fixed in code |
| `order_type` | Limit orders only | fixed in code |
| `market_hours` | Inside the entry window | `NO_ENTRY_OPEN_MINUTES`, `NO_ENTRY_CLOSE_MINUTES` |
| `daily_loss_limit` | Realised P&L of the day (New York date) greater than minus the limit | `MAX_DAILY_LOSS_USD` |
| `monthly_loss_limit` | Realised P&L of the calendar month (New York date) greater than minus the limit; also a sizing limit, see below | `MAX_MONTHLY_LOSS_USD` |
| `max_open_positions` | Open positions plus open entry intents below the maximum | `MAX_OPEN_POSITIONS` |
| `liquidity_open_interest` | Open interest of each leg at least the minimum; missing data rejects | `MIN_OPEN_INTEREST` |
| `liquidity_spread` | Bid-ask spread of each leg, in percent of its mid, at most the maximum; missing data rejects | `MAX_BID_ASK_SPREAD_PCT` |
| `max_position_premium`, `max_portfolio_risk`, `producer_budget`, `underlying_concentration`, `monthly_loss_limit` | Sizing, see below | `MAX_POSITION_USD`, `MAX_PORTFOLIO_RISK_USD`, `budget_usd` in the registry, `MAX_UNDERLYING_EXPOSURE_USD`, `MAX_MONTHLY_LOSS_USD` |

**Sizing.** The cost of one contract (or spread) is computed at the worst case, the **natural price**
(pay the ask of long legs, receive the bid of short legs), times 100. The available premium is the
smallest of five amounts:

1. `min(sizing.max_premium_usd, MAX_POSITION_USD)`
2. `MAX_PORTFOLIO_RISK_USD` minus the premium already at risk (open positions and open entry intents)
3. your producer's `budget_usd` minus your premium at risk
4. `MAX_UNDERLYING_EXPOSURE_USD` minus the premium at risk on that underlying (all producers)
5. `MAX_MONTHLY_LOSS_USD` plus the realised P&L of the calendar month minus the premium at risk
   (all producers), so the month's net loss stays within the limit even if every open position
   loses its whole premium

`quantity = min(sizing.max_contracts, floor(available / cost of one contract))`. If this is less than
1, the entry is rejected with the rule id of the smallest amount (`max_position_premium` also covers
your own `max_premium_usd`). The limit price starts at the mid and is never allowed above
`min(natural, available / quantity / 100)`. Limit prices are rounded to the operator's `PRICE_TICK`
(default 0.05; buys down).

**Manual approval.** Even when risk approves, an entry waits for a human (`awaiting_approval`) if
any of these holds: your producer has status `manual`; the maximum premium of the order exceeds
`MANUAL_APPROVAL_ABOVE_USD`; your `confidence` is below `MIN_CONFIDENCE_AUTO` (disabled at the default 0.0).
The operator approves or rejects with `ibtrader approve|reject <intent_id>`. Without a decision within
`APPROVAL_TIMEOUT_MINUTES` (default 30) the intent expires and no order is placed. The producer
receives no event for the timeout; the intent status changes to `expired`.

**Producer status.** The operator assigns each producer `paper_only`, `manual`, `auto` (or `paused`).
When the operator runs the system in live mode, entries of `paper_only` producers are only simulated
and never sent to IBKR; the events you receive look the same. Do not assume that a filled order
event from a new producer means real money.

## 12. Self-test

Before connecting a producer, test documents offline. No broker, producer registry or network is needed.

```bash
ibtrader validate path/to/signal.md [more.md ...]
```

It prints one JSON object per file (`"status": "valid"` or `"invalid"` with the field-level `errors`)
and exits with 1 if any file is invalid. It runs the same parser and validator as the gateway. It
does **not** check producer identity, `valid_until` against the clock, duplicates, versions,
conflicts, or the risk limits. A document that validates offline can still be acknowledged `expired`,
`duplicate`, `conflict` and so on.

The JSON Schema is [`schemas/signal-contract-1.0.schema.json`](schemas/signal-contract-1.0.schema.json)
(JSON Schema 2020-12; regenerate with `ibtrader schema`). It validates the front matter after YAML
to JSON conversion (quote timestamps so they stay strings). It checks types, ranges, patterns,
enums and unknown keys. It **cannot** check: `short_leg_delta` against `long_leg_delta`, the
spread/single-leg rules, `valid_until` after `created_at`, `close_before_expiry_days` against
`dte_min`, the condition grammar, and the YAML restrictions of section 8. For example, the three
files `spread-delta-order.md`, `code-injection-condition.md` and `yaml-alias.md` pass the schema and
fail `ibtrader validate`. Treat `ibtrader validate` as authoritative.

Examples in `examples/signals/`. The timestamps in them are fixed dates; set them to current values
before sending them to a live inbox, otherwise they are acknowledged `expired` or
`created_in_future`.

| Folder | File | Expected result |
|---|---|---|
| `valid/` | `trade-bull-call-spread.md`, `trade-long-put.md` | valid `trade` (spread, single leg) |
| `valid/` | `rule-condition.md`, `rule-schedule.md` | valid `rule` (condition with `check_at`, schedule; the second one has `version: 2`) |
| `valid/` | `exit-close.md`, `exit-reduce.md` | valid `exit` |
| `invalid/` | `market-order.md` | `order.type`: Input should be 'limit' |
| `invalid/` | `naked-short-call.md` | `strategy`: Input should be 'long_call', 'long_put', 'bull_call_spread' or 'bear_put_spread' |
| `invalid/` | `missing-valid-until.md` | `valid_until`: required key missing |
| `invalid/` | `wrong-type.md` | `contract.dte_min`: Input should be a valid integer |
| `invalid/` | `unknown-key.md` | `sizing.leverage`: unknown key |
| `invalid/` | `spread-delta-order.md` | `$`: contract.short_leg_delta must be smaller than long_leg_delta |
| `invalid/` | `reduce-out-of-range.md` | `reduce_pct`: Input should be less than or equal to 100 |
| `invalid/` | `code-injection-condition.md` | `trigger`: trigger.condition: unexpected character at position 0: '_' |
| `invalid/` | `yaml-alias.md` | `$`: invalid YAML: YAML aliases/anchors are not allowed |
| `invalid/` | `no-front-matter.md` | `$`: document must start with '---' front matter |
| `adversarial/` | `prompt-injection-body.md` | **valid**: instructions in the body are ignored, and the oversized request (`max_premium_usd: 100000`, `max_contracts: 100`) is cut down by the global limits |

## 13. Complete examples

### `trade` (`examples/signals/valid/trade-bull-call-spread.md`)

```markdown
---
contract_version: "1.0"
signal_id: "nvda-momo-2026-10-19-001"
version: 1
kind: trade
producer: "momentum-agent"
created_at: "2026-10-19T13:40:00Z"
valid_until: "2026-10-19T19:55:00Z"
confidence: 0.62
underlying: NVDA
strategy: bull_call_spread
contract:
  dte_min: 21
  dte_max: 45
  long_leg_delta: 0.40
  short_leg_delta: 0.20
order:
  type: limit
  price: mid
  max_reprice_steps: 3
sizing:
  max_premium_usd: 300
  max_contracts: 2
exit:
  take_profit_pct: 50
  stop_loss_pct: 50
  close_before_expiry_days: 5
---

## Rationale
Price closed above 20-day SMA with rising volume; RSI 58.

## Evidence
- Data snapshot time: 2026-10-19T13:35Z
```

### `rule` (`examples/signals/valid/rule-condition.md`)

```markdown
---
contract_version: "1.0"
signal_id: "aapl-sma-cross"
version: 1
kind: rule
producer: "momentum-agent"
created_at: "2026-10-19T12:00:00Z"
valid_until: "2026-11-30T21:00:00Z"
confidence: 0.58
underlying: AAPL
strategy: bull_call_spread
contract:
  dte_min: 21
  dte_max: 45
  long_leg_delta: 0.45
  short_leg_delta: 0.25
order:
  type: limit
  price: mid
  max_reprice_steps: 3
sizing:
  max_premium_usd: 300
  max_contracts: 2
exit:
  take_profit_pct: 50
  stop_loss_pct: 50
  close_before_expiry_days: 5
trigger:
  type: condition
  condition: "close > sma_20 and rsi_14 < 70"
  check_at: "15:30 America/New_York"
max_entries_per_day: 1
---

## Rationale
Trend-following entry once price reclaims the 20-day average without being overbought.
```

### `exit` (`examples/signals/valid/exit-reduce.md`)

```markdown
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
reduce_pct: 50
---

## Rationale
Take half off ahead of the CPI release.
```

## 14. Checklist for agent authors

- [ ] The document starts with `---` on line 1 and the front matter is closed with `---`.
- [ ] `contract_version` is `"1.0"`; `kind` is `trade`, `rule` or `exit`.
- [ ] `producer` equals the identity of the inbox directory or API key you use.
- [ ] `signal_id` matches `^[a-z0-9][a-z0-9._-]{2,127}$` and has never been used for another kind.
- [ ] Timestamps are quoted ISO 8601 with a timezone; `valid_until` is later than `created_at` and
      reaches into an entry window (09:45 to 15:45 New York time with default settings).
- [ ] `trade` and `exit` have `version: 1`; a changed `rule` has a higher `version` than before.
- [ ] Strings that YAML could misread (`"ON"`, `"NO"`) are quoted; numbers are unquoted.
- [ ] Spreads have `short_leg_delta` smaller than `long_leg_delta`; single legs have none.
- [ ] `exit.close_before_expiry_days` is smaller than `contract.dte_min`.
- [ ] `sizing` is what you want to risk at most, in total, for the whole position.
- [ ] Conditions use only the language in section 6, with indicator periods from 2 to 200.
- [ ] A rule's `check_at` lies inside the entry window (not at 09:30 and not at 15:45 or later), and
      the condition cannot stay true in a way that re-enters every day unintentionally.
- [ ] No anchors, aliases, duplicate keys or custom tags; the whole document is below 64 KiB.
- [ ] The body contains rationale only: nothing in it can change trading, and it holds no secrets.
- [ ] `ibtrader validate` accepts the file.
- [ ] You read the acknowledgement of every document and the feedback events, and you treat
      `rejected` with a reason code as final until you have corrected the cause.
- [ ] You do not resend in a loop: each attempt counts toward `rate_limit_per_hour`.
