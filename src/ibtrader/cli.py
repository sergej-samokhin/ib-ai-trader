"""Command line interface: `ibtrader <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import signal as _signal
import sys
import time
from pathlib import Path

from ibtrader import __version__
from ibtrader.audit import setup_logging
from ibtrader.clock import utcnow
from ibtrader.config import Settings, check_mode_safety, load_settings
from ibtrader.contract import SignalParseError, parse_signal
from ibtrader.contract.schema import render_schema
from ibtrader.killswitch import KillSwitch
from ibtrader.notify import build_notifier
from ibtrader.producers import ProducerRegistry, generate_api_key, hash_api_key
from ibtrader.store import Store

log = logging.getLogger("ibtrader.cli")


def _settings(args) -> Settings:
    return load_settings(dotenv=Path(args.env_file) if args.env_file else Path(".env"))


def _store(settings: Settings) -> Store:
    return Store(settings.db_path)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------------------ commands
def cmd_validate(args) -> int:
    """Offline contract check (no producer authentication, no TTL check)."""
    failures = 0
    for name in args.files:
        raw = Path(name).read_text(encoding="utf-8")
        try:
            parsed = parse_signal(raw)
        except SignalParseError as exc:
            failures += 1
            _print({"file": name, "status": "invalid", "errors": [e.as_dict() for e in exc.errors]})
            continue
        sig = parsed.signal
        _print(
            {
                "file": name,
                "status": "valid",
                "kind": sig.kind,
                "signal_id": sig.signal_id,
                "version": sig.version,
            }
        )
    return 1 if failures else 0


def cmd_schema(args) -> int:
    text = render_schema()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def cmd_new_key(args) -> int:
    key = generate_api_key()
    _print(
        {
            "api_key": key,
            "api_key_sha256": hash_api_key(key),
            "note": "give api_key to the producer; put only api_key_sha256 in producers.yaml",
        }
    )
    return 0


def cmd_kill(args) -> int:
    settings = _settings(args)
    switch = KillSwitch(settings.kill_switch_file)
    if args.action == "on":
        switch.activate(args.reason or "manual", utcnow())
    elif args.action == "off":
        switch.deactivate()
    _print({"kill_switch": "active" if switch.is_active() else "off", "reason": switch.reason()})
    return 0


def cmd_producer(args) -> int:
    settings = _settings(args)
    store = _store(settings)
    now = utcnow()
    if args.action == "pause":
        store.pause_producer(args.producer, args.reason or "manual", now)
        store.append_audit(now, "producer_paused", {"reason": args.reason}, producer=args.producer)
    elif args.action == "resume":
        store.resume_producer(args.producer)
        store.append_audit(now, "producer_resumed", {}, producer=args.producer)
    registry = ProducerRegistry.load(settings.producers_file)
    _print(
        [
            {
                "producer": p.id,
                "status": p.status.value,
                "budget_usd": p.budget_usd,
                "paused": store.is_producer_paused(p.id),
            }
            for p in registry.all()
        ]
    )
    return 0


def _offline_trader(settings: Settings):
    """Trader without a broker connection, for approve/reject bookkeeping."""
    from ibtrader.app import Trader
    from ibtrader.sim import FakeMarketData, SimBroker

    md = FakeMarketData()
    return Trader(
        settings,
        ProducerRegistry.load(settings.producers_file),
        _store(settings),
        md,
        SimBroker(md),
        build_notifier(settings),
    )


def cmd_approve(args) -> int:
    trader = _offline_trader(_settings(args))
    method = trader.approve if args.command == "approve" else trader.reject
    ok = method(args.intent_id, args.by, utcnow())
    _print({"intent_id": args.intent_id, "ok": ok})
    return 0 if ok else 1


def cmd_intent(args) -> int:
    """Resolve an `unknown` intent after checking the order in TWS."""
    trader = _offline_trader(_settings(args))
    if args.filled is None:
        print("--filled is required (0 if the order did not fill)", file=sys.stderr)
        return 2
    try:
        status = trader.resolve_intent(args.intent_id, args.filled, args.price, args.by, utcnow())
    except ValueError as exc:
        _print({"intent_id": args.intent_id, "ok": False, "error": str(exc)})
        return 1
    _print({"intent_id": args.intent_id, "ok": True, "status": status})
    return 0


def cmd_position(args) -> int:
    """Book a close that happened outside ibtrader."""
    trader = _offline_trader(_settings(args))
    try:
        pnl = trader.close_position_manually(
            args.position_id, args.price, args.by, utcnow(), quantity=args.quantity
        )
    except ValueError as exc:
        _print({"position_id": args.position_id, "ok": False, "error": str(exc)})
        return 1
    _print({"position_id": args.position_id, "ok": True, "realized_pnl": round(pnl, 2)})
    return 0


def cmd_status(args) -> int:
    settings = _settings(args)
    store = _store(settings)
    _print(
        {
            "mode": settings.mode,
            "kill_switch": KillSwitch(settings.kill_switch_file).is_active(),
            "open_positions": [dict(r) for r in store.open_positions()],
            "open_intents": [
                dict(r)
                for r in store.intents_with_status(
                    "awaiting_approval", "approved", "submitted", "unknown"
                )
            ],
            "active_signals": [
                {
                    k: r[k]
                    for k in ("producer", "signal_id", "version", "kind", "status", "valid_until")
                }
                for r in store.signals_with_status("pending", "active")
            ],
        }
    )
    return 0


def cmd_scorecard(args) -> int:
    _print(_store(_settings(args)).scorecard())
    return 0


def cmd_submit(args) -> int:
    """Push a local file through the gateway as `producer` (transport 'cli'; for operators)."""
    settings = _settings(args)
    trader = _offline_trader(settings)
    raw = Path(args.file).read_text(encoding="utf-8")
    ack = trader.gateway.submit(raw, args.producer, "cli", utcnow())
    _print(ack.as_dict())
    return 0 if ack.status in ("accepted", "duplicate") else 1


def _build_live_trader(settings: Settings):
    from ibtrader.app import Trader
    from ibtrader.ibkr import IBKRBroker, IBKRConnection, IBKRMarketData

    conn = IBKRConnection(settings)
    conn.connect()
    md = IBKRMarketData(conn)
    trader = Trader(
        settings,
        ProducerRegistry.load(settings.producers_file),
        _store(settings),
        md,
        IBKRBroker(conn),
        build_notifier(settings),
    )
    return trader, conn


def cmd_reconcile(args) -> int:
    settings = _settings(args)
    trader, conn = _build_live_trader(settings)
    diff = trader.reconcile()
    _print({"differences": {str(k): {"expected": v[0], "broker": v[1]} for k, v in diff.items()}})
    conn.ib.disconnect()
    return 1 if diff else 0


def cmd_run(args) -> int:
    settings = _settings(args)
    if args.demo:  # never mix simulated positions into the real state store
        settings = settings.model_copy(update={"data_dir": settings.data_dir / "demo"})
    setup_logging(settings.log_file)
    problems = check_mode_safety(settings)
    if problems and not args.demo:
        for problem in problems:
            log.error("unsafe configuration", extra={"fields": {"problem": problem}})
        return 2

    from ibtrader.app import Trader, run_iteration
    from ibtrader.health import ConnectionWatch
    from ibtrader.ingestion.filedrop import FileDropAdapter
    from ibtrader.ingestion.http import HttpAdapter

    registry = ProducerRegistry.load(settings.producers_file)
    if not registry.all():
        log.error(
            "no producers registered", extra={"fields": {"file": str(settings.producers_file)}}
        )
        return 2

    conn = None
    if args.demo:
        from ibtrader.sim import FakeMarketData, SimBroker

        md = FakeMarketData()
        trader = Trader(
            settings, registry, _store(settings), md, SimBroker(md), build_notifier(settings)
        )
        sleep = time.sleep
        log.warning("DEMO MODE: synthetic market data, simulated broker, nothing reaches IBKR")
    else:
        trader, conn = _build_live_trader(settings)
        sleep = conn.sleep
        log.warning("starting", extra={"fields": {"mode": settings.mode, "account": conn.account}})

    trader.recover(utcnow())
    adapters = []
    if settings.file_drop_enabled:
        file_adapter = FileDropAdapter(settings, trader.gateway)
        file_adapter.ensure_dirs([p.id for p in registry.all()])
        trader.feedback_sinks.append(file_adapter.write_feedback)
        adapters.append(file_adapter)
    http = None
    if settings.http_enabled:
        http = HttpAdapter(settings, registry, trader.gateway, trader.store)
        http.start()

    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)
    trader.notifier.send("ibtrader started", f"mode={settings.mode} demo={args.demo}")
    watch = ConnectionWatch(trader.notifier, trader.audit)
    while not stop["flag"]:
        now = utcnow()
        try:
            run_iteration(
                trader, adapters, watch, now, conn.ensure_connected if conn is not None else None
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("loop error")
            trader.notifier.send("Loop error", str(exc), "error")
        if args.once:
            break
        sleep(settings.loop_interval_seconds)
    if http:
        http.stop()
    if conn is not None:
        conn.ib.disconnect()
    trader.notifier.send("ibtrader stopped", "clean shutdown")
    return 0


# ------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ibtrader", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", default=None, help="path to .env (default ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run the trading loop")
    p.add_argument("--demo", action="store_true", help="synthetic data + simulated broker")
    p.add_argument("--once", action="store_true", help="single iteration (smoke test)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("validate", help="validate signal files against the contract (offline)")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("submit", help="submit a signal file through the gateway as a producer")
    p.add_argument("file")
    p.add_argument("--producer", required=True)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("schema", help="print / write the Signal Contract JSON Schema")
    p.add_argument("--out")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("new-key", help="generate a producer API key and its hash")
    p.set_defaults(func=cmd_new_key)

    p = sub.add_parser("kill", help="kill switch")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--reason")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("producer", help="pause / resume / list producers")
    p.add_argument("action", choices=["pause", "resume", "list"])
    p.add_argument("producer", nargs="?")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_producer)

    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} an intent awaiting manual approval")
        p.add_argument("intent_id")
        p.add_argument("--by", default="operator")
        p.set_defaults(func=cmd_approve)

    p = sub.add_parser("intent", help="resolve an intent left 'unknown' after a restart")
    p.add_argument("action", choices=["resolve"])
    p.add_argument("intent_id")
    p.add_argument("--filled", type=int, help="contracts filled at IBKR (0 = not filled)")
    p.add_argument("--price", type=float, help="average fill price per share (net for spreads)")
    p.add_argument("--by", default="operator")
    p.set_defaults(func=cmd_intent)

    p = sub.add_parser("position", help="book a position closed outside ibtrader (e.g. in TWS)")
    p.add_argument("action", choices=["close"])
    p.add_argument("position_id")
    p.add_argument("--price", type=float, required=True, help="exit price per share (net credit)")
    p.add_argument("--quantity", type=int, help="contracts closed (default: all)")
    p.add_argument("--by", default="operator")
    p.set_defaults(func=cmd_position)

    p = sub.add_parser("status", help="open positions, open intents, active signals")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("scorecard", help="per-producer scorecard")
    p.set_defaults(func=cmd_scorecard)

    p = sub.add_parser("reconcile", help="compare stored positions with IBKR positions")
    p.set_defaults(func=cmd_reconcile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "producer" and args.action in ("pause", "resume") and not args.producer:
        print("producer id required", file=sys.stderr)
        return 2
    return int(args.func(args) or 0)
