# ADR 0001: Signal ingestion

- Status: Accepted
- Date: 2026-10-18
- Scope: how signals from upstream AI agents reach the trader (build spec section 3)

## Context

Signals arrive as Markdown documents with YAML front matter, produced by several independent
upstream agents that we do not control, possibly through different channels. The trader acts on
real money (paper by default), so the ingestion path is the main trust boundary of the system.

The spec (section 3) asks for: a transport-agnostic core; at least one adapter in v1; producer
authentication with unknown producers rejected; acknowledgement statuses `accepted`, `rejected`,
`duplicate`, `expired`; idempotency per `signal_id` and `version`; ordering and updates; backpressure by
per-producer rate limit; auditability with the raw payload stored verbatim; and the rule that signal
content is data, never code. It leaves the choice of transport to the implementer and asks for this ADR.

Facts about the deployment that shape the decision:

- The trader runs as a single process next to IB Gateway on one always-on host (single-threaded
  loop, SQLite). It is not a multi-node service.
- The expected producers are a small number of agents operated by the account owner or by people the
  owner knows. Signals are few (tens per day, not thousands per second).
- Some producers may run elsewhere (a cloud agent runtime) and cannot share a filesystem with the host.

## Decision

1. **The core is transport-agnostic.** Both transports call one function:
   `SignalGateway.submit(raw: str, producer_id: str, transport: str, now) -> Ack`
   (`src/ibtrader/gateway.py`). `producer_id` is the identity that the transport has already proven.
   Adapters read bytes, authenticate, call `submit`, and report the `Ack`. They contain no validation,
   deduplication, risk or trading logic. A further transport is an additional adapter and requires no
   change to the core.

2. **File drop is the v1 default** (`src/ibtrader/ingestion/filedrop.py`, enabled by `FILE_DROP_ENABLED`, default `true`).
   - Layout under `DATA_DIR` (default `var/`):
     `inbox/<producer>/*.md` (producer writes), `outbox/<producer>/<signal_id>.jsonl` (acknowledgement
     and lifecycle feedback, one JSON line per event, producer reads), `processed/<producer>/`
     (accepted and duplicate payloads), `quarantine/<producer>/` (rejected and expired payloads). Moved
     files get a UTC timestamp prefix.
   - **Identity is the inbox directory.** It is enforced by operating-system permissions: one Unix
     user and group per producer, with write access only to its own inbox and read access only to its own
     outbox. The software does not set or verify these permissions; it uses the directory name as the
     identity. Example layout (adapt and verify; not enforced by the code):

     ```text
     var/inbox/            owner ibtrader   mode 0711   (traverse, no listing)
     var/inbox/<id>/       owner <producer-user>, group ibtrader   mode 0770
     var/outbox/           owner ibtrader   mode 0711
     var/outbox/<id>/      owner ibtrader, group <producer-group>  mode 0750
     ```

     The trader needs write permission on the inbox directory because it moves processed files out of it.
   - Only regular `*.md` files directly in `inbox/<producer>/` are read. Symbolic links are skipped.
     Files with a modification time younger than 1 second are skipped to avoid reading half-written files;
     producers should write to a temporary name and rename. The adapter reads at most `MAX_SIGNAL_BYTES + 1` bytes.
   - The adapter is polled by the main loop every `LOOP_INTERVAL_SECONDS` (default 5).
   - A directory under `inbox/` that is not a registered producer is rejected by the gateway
     (`unknown_producer`), quarantined, and gets no outbox entry.

3. **An authenticated HTTP adapter is optional** (`src/ibtrader/ingestion/http.py`, `HTTP_ENABLED`,
   default `false`), for producers that cannot share a filesystem.
   - `POST /v1/signals` with header `Authorization: Bearer <api key>` and the Markdown document as the
     body; `GET /v1/signals/<signal_id>` for the status history and order intents of the caller's own
     signals; `GET /healthz`.
   - Credentials: `ibtrader new-key` generates a key (`ibt_` plus 32 random bytes, URL-safe) and its
     SHA-256. **Only the hash** (`api_key_sha256`) is stored in `config/producers.yaml`. The presented key
     is hashed and compared with `hmac.compare_digest` against the stored hashes.
   - Response codes: `202` accepted, `200` duplicate, `422` rejected or expired, `429` rate limited,
     `401` unauthenticated. Also `411` (invalid `Content-Length`) and `404`. The body is the acknowledgement JSON.
   - The adapter speaks plain HTTP. It binds to `HTTP_HOST` (default `127.0.0.1`, port `HTTP_PORT`
     8787). Remote producers must reach it through a TLS-terminating reverse proxy that the operator
     runs; the application provides no TLS.

4. **Registry.** Producers are declared in `config/producers.yaml` (`status`, `budget_usd`,
   `rate_limit_per_hour`, `api_key_sha256`, `description`). It is read once at startup.

### How each requirement of spec section 3 is met

| Requirement | Implementation |
|---|---|
| Transport-agnostic core | Single `SignalGateway.submit`; the transport name (`file`, `http`, `cli`) is only recorded with the payload. |
| At least one adapter in v1 | File drop (default) and HTTP (optional). |
| Producer authentication | File drop: directory plus OS permissions. HTTP: bearer key, hash only, constant-time compare. In both cases the document's `producer` field must equal the proven identity (`producer_mismatch`), and identities that are not in the registry are rejected (`unknown_producer`). |
| Acknowledgement | `Ack` with status `accepted`, `rejected` (reason code and field-level `errors`), `duplicate`, `expired`. File drop: first line in the outbox file. HTTP: response body and code. Lifecycle events follow in the outbox file (file drop) or via `GET`. |
| Idempotency | `(producer, signal_id, version)` is the primary key of the `signals` table; a repeat is acknowledged `duplicate` and not processed again. |
| Ordering and updates | A `rule` with a higher `version` supersedes the old one; a lower version is `stale_version`. `trade` and `exit` must have `version: 1` and are immutable. |
| Backpressure | Per-producer limit `rate_limit_per_hour` (default 60), counted over the trailing 60 minutes from the stored raw payloads. Excess documents are rejected with `rate_limited` (HTTP 429), not queued. There is no queue. Payload size is capped by `MAX_SIGNAL_BYTES`. |
| Auditability | Every payload is written to the `raw_signals` table before anything else (receipt time, transport, producer, SHA-256, size, truncation flag, payload text, final status and errors), whatever the outcome. Decisions go to the `audit_log` table, which is append-only (SQLite triggers abort UPDATE and DELETE), and to JSON logs. |
| Security: content is data | YAML goes through a `SafeLoader` subclass that rejects aliases, anchors and duplicate keys and keeps timestamps as strings; then strict pydantic models with `extra="forbid"` (no coercion). `trigger.condition` goes through a whitelist parser (no calls, no attribute access, no strings). The Markdown body is stored and shown to the operator as text; it is never parsed or acted on. No `eval`, no shell, no following of links. Size is capped before parsing. File drop skips symbolic links and non-regular files. |

## Consequences

Positive:

- The default needs no network listener at all. Authentication is the operating system's, so there
  is no key to leak or rotate for local producers.
- Files in `processed/` and `quarantine/` plus the `raw_signals` table give a complete, replayable
  record of what each producer sent.
- Adding a transport is local to one adapter.
- Bursts are bounded by the rate limit and the size limit; the trader never builds a backlog of signals.

Negative and known gaps (v0.1):

- **File drop requires a shared host** (or a shared filesystem). Remote agents must use HTTP.
- **HTTP has no TLS** of its own and no payload signing. A bearer key that is sniffed on an
  unencrypted hop can be replayed. Run it on localhost or behind a TLS reverse proxy.
- **No HMAC (or other) signature on payloads.** Integrity and origin rest on the transport. Replay
  of an intercepted document is limited by `valid_until`, the duplicate check and, for `trade`, by the
  fact that a second delivery with the same `signal_id` is a duplicate.
- **The registry is loaded once at startup.** Adding a producer, creating its directories and
  revoking a key all need a restart. In the meantime `ibtrader producer pause <id>` takes effect
  immediately (it is stored in the database).
- **Permissions are the operator's job.** The software creates directories with default
  permissions (`ensure_dirs` at startup) and does not check them. A world-writable inbox means any local
  user can impersonate that producer.
- **Unauthenticated traffic is only logged, not audited.** A failed HTTP authentication (`401`) appears
  in the JSON log as an access line but not in `audit_log` or `raw_signals`, and is not rate limited. The
  HTTP server (`ThreadingHTTPServer`) sets no socket timeouts.
- **The rate limit applies to registered producers only.** Documents from an unregistered directory
  are stored in `raw_signals` and rejected before the rate limit is applied; a flood into a
  made-up directory grows the database.
- Every document counts against the rate limit, including rejected ones and the ones that were
  rejected for exceeding it.
- Latency: file drop adds up to `LOOP_INTERVAL_SECONDS` plus the 1 second stability check.
- "Verbatim" means the bytes decoded as UTF-8 with replacement characters; for payloads larger than
  `MAX_SIGNAL_BYTES` the stored text is truncated (and flagged) and the SHA-256 covers the part that was read.
- Outbox, processed and quarantine directories grow without limit; there is no cleanup job.
- Lifecycle feedback is pushed only through the outbox. The HTTP adapter has no push channel; HTTP
  producers poll `GET /v1/signals/<id>` (which does not return audit events, only status history and intents).
- The conflict rejection message names the other producer's active `signal_id`, which discloses it.

## Alternatives considered

| Alternative | Why not in v1 |
|---|---|
| **Message queue** (for example RabbitMQ, Redis streams, SQS) | Adds a service with its own authentication, TLS and operations for a handful of low-volume producers on a single host. Durable delivery and fan-out are not needed because the gateway is already idempotent and the trader is single-process. Can be added later as an adapter that calls `submit`. |
| **Git repository poll** | Gives history and review for free, but identity would rest on repository credentials or commit signatures, latency depends on the poll interval, rate limiting is awkward, and the trader would need repository credentials. The file-drop directories already provide the audit trail. |
| **MCP tool exposed to agents** | Convenient for agents that run inside an MCP-capable runtime, but it couples the trader to one agent runtime and would need its own authentication, rate limiting and feedback model. Not needed to start; compatible with the design as a future adapter (an MCP server can call the HTTP adapter or `submit`). |
| **Signed payloads (HMAC or public-key signatures)** | Useful when documents pass through untrusted intermediaries or when the transport cannot authenticate. It needs key distribution, rotation and canonicalisation rules. With direct delivery over a protected transport it adds little. Deferred; the document format is unchanged by adding it later (for example a detached signature header in the HTTP adapter). |
| **HTTP only** | Would force every producer, including local ones, to manage and protect a key and would add a network listener to the default deployment. |
| **File drop only** | Excludes producers on other hosts. |

## Related

- [`SIGNAL_CONTRACT.md`](../../SIGNAL_CONTRACT.md): what producers send and receive
- [`docs/RUNBOOK.md`](../RUNBOOK.md): pausing a producer, onboarding checks
- [`docs/ASSUMPTIONS.md`](../ASSUMPTIONS.md): open decisions and limitations
