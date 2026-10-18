"""File-drop adapter (v1 default transport, see docs/adr/0001-signal-ingestion.md).

Layout under DATA_DIR:
    inbox/<producer>/*.md        producer writes here (OS permissions = identity)
    outbox/<producer>/*.json     acks + lifecycle feedback for the producer to read
    processed/<producer>/        accepted / duplicate payloads, moved after processing
    quarantine/<producer>/       rejected / expired payloads

The adapter holds no business logic: it reads bytes and hands them to the gateway.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from ibtrader.clock import utcnow
from ibtrader.config import Settings
from ibtrader.gateway import Ack, SignalGateway

log = logging.getLogger("ibtrader.ingestion.file")
STABLE_SECONDS = 1.0


class FileDropAdapter:
    transport = "file"

    def __init__(self, settings: Settings, gateway: SignalGateway):
        self.settings = settings
        self.gateway = gateway
        self.inbox = settings.inbox_dir
        self.outbox = settings.outbox_dir

    def ensure_dirs(self, producer_ids: list[str]) -> None:
        for base in (
            self.inbox,
            self.outbox,
            self.settings.processed_dir,
            self.settings.quarantine_dir,
        ):
            base.mkdir(parents=True, exist_ok=True)
        for pid in producer_ids:
            (self.inbox / pid).mkdir(exist_ok=True)
            (self.outbox / pid).mkdir(exist_ok=True)

    def poll(self, now: datetime) -> list[tuple[Path, Ack]]:
        results: list[tuple[Path, Ack]] = []
        if not self.inbox.is_dir():
            return results
        for producer_dir in sorted(p for p in self.inbox.iterdir() if p.is_dir()):
            if producer_dir.is_symlink():
                continue
            for path in sorted(producer_dir.glob("*.md")):
                if path.is_symlink() or not path.is_file():
                    log.warning("skipping non-regular file", extra={"fields": {"path": str(path)}})
                    continue
                stat = path.stat()
                if now.timestamp() - stat.st_mtime < STABLE_SECONDS:
                    continue  # still being written
                ack = self._ingest(producer_dir.name, path, now)
                results.append((path, ack))
        return results

    def _ingest(self, producer_id: str, path: Path, now: datetime) -> Ack:
        limit = self.settings.max_signal_bytes
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
        raw = data.decode("utf-8", errors="replace")  # > limit bytes is rejected by the gateway
        ack = self.gateway.submit(raw, producer_id, self.transport, now)
        target_root = (
            self.settings.processed_dir
            if ack.status in ("accepted", "duplicate")
            else self.settings.quarantine_dir
        )
        target_dir = target_root / producer_id
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        os.replace(path, target_dir / f"{stamp}-{path.name}")
        self.write_feedback(producer_id, {"event": "ack", "file": path.name, **ack.as_dict()})
        return ack

    def write_feedback(self, producer_id: str, payload: dict) -> None:
        """Append one JSON line per event to outbox/<producer>/<signal_id>.jsonl."""
        target_dir = self.outbox / producer_id
        if not target_dir.is_dir():
            if self.gateway.registry.get(producer_id) is None:
                return  # never create directories for unknown identities
            target_dir.mkdir(parents=True, exist_ok=True)
        name = payload.get("signal_id") or Path(payload.get("file", "unknown")).stem
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "._-")[:128] or "unknown"
        line = json.dumps({"at": utcnow().isoformat(), **payload}, default=str)
        with (target_dir / f"{safe}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
