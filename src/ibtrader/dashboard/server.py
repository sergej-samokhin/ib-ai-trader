"""Loopback-only HTTP server for the dashboard (stdlib, no dependencies).

    GET  /, /app.js, /style.css       static UI
    GET  /api/overview                 mode, kill switch, P&L, positions, intents, signals
    GET  /api/audit?limit=100|250|500  latest audit events
    GET  /api/radar[?demo=1]           WSB radar snapshot (demo = synthetic data)
    POST /api/radar/scan               start one radar scan (same-origin browser requests only)

There is no login, so the server refuses to bind anything but a loopback address. Reach it from
another machine through an SSH tunnel. The Host header is checked against the bound address
(DNS-rebinding guard), and the only POST needs a matching Origin header.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ibtrader.config import Settings
from ibtrader.dashboard import api
from ibtrader.radar.service import Radar

log = logging.getLogger("ibtrader.dashboard")
SCAN_COOLDOWN_SECONDS = 30
STATIC = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _static(name: str) -> bytes:
    return resources.files("ibtrader.dashboard").joinpath("static", name).read_bytes()


def check_loopback(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise ValueError(f"dashboard has no authentication; refusing to bind {host!r}")


def make_handler(
    settings: Settings,
    radar: Radar,
    clock: Callable[[], datetime],
    allowed_hosts: set[str],
) -> type[BaseHTTPRequestHandler]:
    def scan_now() -> None:
        try:
            radar.scan(clock())
        except Exception:  # noqa: BLE001 - background thread; status carries the error
            log.exception("radar scan crashed")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ibtrader-dashboard"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:
            log.debug("http", extra={"fields": {"line": fmt % args}})

        def _send(self, code: int, body: bytes | Any, content_type: str = "application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, default=str, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(403, {"error": "invalid host"})
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path in STATIC:
                name, kind = STATIC[parsed.path]
                self._send(200, _static(name), kind)
                return
            try:
                if parsed.path == "/api/overview":
                    store = api.open_trading_store(settings)
                    try:
                        self._send(200, api.overview(settings, store, clock()))
                    finally:
                        if store is not None:
                            store.close()
                elif parsed.path == "/api/audit":
                    try:
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError:
                        limit = 100
                    store = api.open_trading_store(settings)
                    try:
                        self._send(200, api.audit(store, limit))
                    finally:
                        if store is not None:
                            store.close()
                elif parsed.path == "/api/radar":
                    demo = query.get("demo", ["0"])[0] == "1"
                    self._send(200, radar.snapshot(clock(), demo_mode=demo))
                else:
                    self._send(404, {"error": "not found"})
            except Exception:  # noqa: BLE001 - never leak internals to the browser
                log.exception("dashboard request failed")
                self._send(500, {"error": "unable to load data"})

        def do_POST(self) -> None:  # noqa: N802
            host = self.headers.get("Host", "")
            if not self._host_ok() or self.headers.get("Origin") != f"http://{host}":
                self._send(403, {"error": "same-origin request required"})
                return
            if urlsplit(self.path).path != "/api/radar/scan":
                self._send(404, {"error": "not found"})
                return
            if not radar.settings.enabled:
                self._send(409, {"error": "the radar is disabled (RADAR_ENABLED=false)"})
                return
            status = radar.snapshot(clock())["status"]
            now = clock().timestamp()
            if radar.running or now - (status.get("last_attempt") or 0) < SCAN_COOLDOWN_SECONDS:
                self._send(409, {"error": "a scan is running or was started less than 30 s ago"})
                return
            threading.Thread(target=scan_now, name="radar-scan", daemon=True).start()
            self._send(202, {"accepted": True})

    return Handler


def serve(
    settings: Settings,
    radar: Radar,
    clock: Callable[[], datetime],
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create (not start) the server. The caller runs ``serve_forever`` and shuts it down."""
    check_loopback(host)
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    bound = server.server_address[1]
    names = {host, "127.0.0.1", "localhost"} if host != "::1" else {"[::1]", "localhost"}
    allowed = {f"{name}:{bound}" for name in names}
    server.RequestHandlerClass = make_handler(settings, radar, clock, allowed)
    return server
