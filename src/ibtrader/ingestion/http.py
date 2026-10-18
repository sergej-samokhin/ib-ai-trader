"""Authenticated HTTP adapter (optional; for producers that cannot share a filesystem).

    POST /v1/signals            Authorization: Bearer <key>, body = Markdown signal
    GET  /v1/signals/<id>       status history + intents for one of *your* signals
    GET  /healthz

Bind to localhost or put it behind a TLS-terminating reverse proxy; it speaks plain HTTP.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ibtrader.clock import utcnow
from ibtrader.config import Settings
from ibtrader.gateway import SignalGateway
from ibtrader.producers import ProducerRegistry
from ibtrader.store import Store

log = logging.getLogger("ibtrader.ingestion.http")
_STATUS_PATH = re.compile(r"^/v1/signals/([a-z0-9][a-z0-9._-]{2,127})$")
_HTTP_CODES = {"accepted": 202, "duplicate": 200, "expired": 422, "rejected": 422}


def make_handler(
    settings: Settings, registry: ProducerRegistry, gateway: SignalGateway, store: Store
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ibtrader"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:
            log.info(
                "http", extra={"fields": {"client": self.client_address[0], "line": fmt % args}}
            )

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _producer(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            return registry.authenticate_key(header.removeprefix("Bearer ").strip())

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"ok": True})
                return
            match = _STATUS_PATH.match(self.path)
            if not match:
                self._send(404, {"error": "not found"})
                return
            producer = self._producer()
            if producer is None:
                self._send(401, {"error": "unauthorized"})
                return
            signal_id = match.group(1)
            history = [dict(r) for r in store.signal_history(producer.id, signal_id)]
            intents = [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "kind",
                        "status",
                        "rule",
                        "reason",
                        "quantity",
                        "filled_qty",
                        "avg_fill_price",
                        "updated_at",
                    )
                }
                for r in store.query(
                    "SELECT * FROM intents WHERE producer = ? AND signal_id = ?"
                    " ORDER BY created_at",
                    (producer.id, signal_id),
                )
            ]
            if not history and not intents:
                self._send(404, {"error": "unknown signal"})
                return
            self._send(200, {"signal_id": signal_id, "history": history, "intents": intents})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/signals":
                self._send(404, {"error": "not found"})
                return
            producer = self._producer()
            if producer is None:
                self._send(401, {"status": "rejected", "reason": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0:
                self._send(411, {"status": "rejected", "reason": "content_length_required"})
                return
            # Read at most limit+1 bytes; the gateway records and rejects oversize payloads.
            raw = self.rfile.read(min(length, settings.max_signal_bytes + 1))
            ack = gateway.submit(
                raw.decode("utf-8", errors="replace"), producer.id, "http", utcnow()
            )
            code = 429 if ack.reason == "rate_limited" else _HTTP_CODES[ack.status]
            self._send(code, ack.as_dict())

    return Handler


class HttpAdapter:
    transport = "http"

    def __init__(
        self, settings: Settings, registry: ProducerRegistry, gateway: SignalGateway, store: Store
    ):
        handler = make_handler(settings, registry, gateway, store)
        self.server = ThreadingHTTPServer((settings.http_host, settings.http_port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
