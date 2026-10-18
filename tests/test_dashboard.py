"""Dashboard: read-only payloads, HTTP guards, static files; radar isolation from trading."""

from __future__ import annotations

import ast
import http.client
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ibtrader.config import Settings
from ibtrader.dashboard import api
from ibtrader.dashboard.server import check_loopback, serve
from ibtrader.radar.config import RadarSettings
from ibtrader.radar.service import Radar
from ibtrader.radar.store import RadarStore
from ibtrader.store import Store

NOW = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[1] / "src" / "ibtrader"


def _seed(settings: Settings) -> None:
    store = Store(settings.db_path)
    raw = store.record_raw(
        received_at=NOW,
        transport="file",
        producer="social",
        sha256="x",
        size=1,
        payload="---",
        truncated=False,
    )
    store.insert_signal(
        producer="social",
        signal_id="s-1",
        version=1,
        kind="trade",
        underlying="NVDA",
        strategy="bull_call_spread",
        status="active",
        valid_until=NOW,
        data={},
        body="",
        raw_id=raw,
        received_at=NOW,
    )
    legs = json.dumps(
        [
            {
                "symbol": "NVDA",
                "expiry": "2026-11-20",
                "strike": 25.0,
                "right": "C",
                "action": "BUY",
                "ratio": 1,
            }
        ]
    )
    store.insert_intent(
        NOW,
        producer="social",
        signal_id="s-1",
        version=1,
        kind="entry",
        underlying="NVDA",
        strategy="bull_call_spread",
        legs=legs,
        quantity=1,
        status="awaiting_approval",
        shadow=0,
    )
    pos = store.insert_position(
        NOW,
        intent_id="i",
        producer="social",
        signal_id="s-1",
        version=1,
        underlying="NVDA",
        strategy="bull_call_spread",
        legs=legs,
        quantity=1,
        entry_price=1.2,
        exit_rules="{}",
        shadow=1,
    )
    store.record_close(pos, 1, 0.7, NOW)
    store.append_audit(NOW, "signal_accepted", {"note": "<b>x</b>"}, producer="social")
    store.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "var")


def test_overview_without_database(settings):
    payload = api.overview(settings, api.open_trading_store(settings), NOW)
    assert payload["db_available"] is False and payload["open_positions"] == []
    assert not settings.db_path.exists()  # looking does not create the database


def test_overview_and_audit_read_the_trading_store(settings):
    _seed(settings)
    store = api.open_trading_store(settings)
    payload = api.overview(settings, store, NOW)
    assert payload["db_available"] and payload["mode"] == "paper" and not payload["kill_switch"]
    assert payload["active_signals"][0]["signal_id"] == "s-1"
    assert payload["open_intents"][0]["status"] == "awaiting_approval"
    assert payload["open_intents"][0]["shadow"] is False
    assert payload["pnl"]["today_shadow"] == pytest.approx(-50.0) and payload["pnl"]["today"] == 0
    assert payload["scorecard"][0]["producer"] == "social"
    events = api.audit(store, 100)["events"]
    assert events[0]["event"] == "signal_accepted" and events[0]["data"] == {"note": "<b>x</b>"}
    assert len(api.audit(store, 7)["events"]) == 1  # unknown limit falls back to 100
    with pytest.raises(sqlite3.OperationalError):
        store.execute("DELETE FROM kv")
    store.close()


def test_kill_switch_is_reported(settings):
    settings.kill_switch_file.parent.mkdir(parents=True)
    settings.kill_switch_file.write_text("test")
    assert api.overview(settings, None, NOW)["kill_switch"] is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])  # noqa: S104
def test_refuses_non_loopback_bind(host):
    with pytest.raises(ValueError):
        check_loopback(host)


@pytest.fixture
def server(settings, tmp_path):
    radar = Radar(
        RadarSettings(enabled=True, provider="demo", catalog_refresh=False),
        tmp_path / "radar",
        store=RadarStore(":memory:"),
    )
    srv = serve(settings, radar, lambda: NOW, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _call(srv, method="GET", path="/", headers=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_static_and_api_routes(server):
    resp, body = _call(server, path="/")
    assert resp.status == 200 and b"app.js" in body
    assert "script-src 'self'" in resp.getheader("Content-Security-Policy")
    assert _call(server, path="/app.js")[0].status == 200
    assert _call(server, path="/style.css")[0].status == 200
    assert json.loads(_call(server, path="/api/overview")[1])["db_available"] is False
    radar = json.loads(_call(server, path="/api/radar?demo=1")[1])
    assert radar["mode"] == "demo" and radar["tickers"]
    assert _call(server, path="/api/nope")[0].status == 404
    assert _call(server, path="/../../etc/passwd")[0].status == 404


def test_host_and_origin_guards(server):
    port = server.server_address[1]
    assert _call(server, path="/api/overview", headers={"Host": "evil.test"})[0].status == 403
    assert _call(server, "POST", "/api/radar/scan")[0].status == 403
    cross = {"Origin": "http://evil.test"}
    assert _call(server, "POST", "/api/radar/scan", cross)[0].status == 403
    same = {"Origin": f"http://127.0.0.1:{port}"}
    assert _call(server, "POST", "/api/radar/scan", same)[0].status == 202


# ------------------------------------------------------------------------------ isolation
def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_trading_core_never_imports_radar_or_dashboard():
    allowed = {"cli.py"}  # the CLI wires the dashboard command, nothing else
    for path in SRC.glob("*.py"):
        if path.name in allowed:
            continue
        bad = {n for n in _imports(path) if n.startswith(("ibtrader.radar", "ibtrader.dashboard"))}
        assert not bad, f"{path.name} imports {bad}"
    for sub in ("contract", "ingestion"):
        for path in (SRC / sub).glob("*.py"):
            assert not any(n.startswith("ibtrader.radar") for n in _imports(path)), path


def test_radar_and_dashboard_import_nothing_that_trades():
    forbidden = (
        "ibtrader.orders",
        "ibtrader.ibkr",
        "ibtrader.risk",
        "ibtrader.app",
        "ibtrader.gateway",
        "ibtrader.selector",
        "ibtrader.sim",
        "ib_async",
    )
    for folder in ("radar", "dashboard"):
        for path in (SRC / folder).glob("*.py"):
            bad = {n for n in _imports(path) if n.startswith(forbidden)}
            assert not bad, f"{folder}/{path.name} imports {bad}"
