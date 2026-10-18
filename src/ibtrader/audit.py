"""Structured JSON logging plus the append-only audit trail (DB + JSONL)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ibtrader.store import Store, iso

LOGGER_NAME = "ibtrader"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_file: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter())
    logger.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


log = logging.getLogger(LOGGER_NAME)


class Audit:
    """Every decision goes through here: one DB row (append-only) + one JSON log line."""

    def __init__(self, store: Store):
        self.store = store

    def __call__(
        self,
        event: str,
        now: datetime,
        *,
        producer: str | None = None,
        signal_id: str | None = None,
        version: int | None = None,
        intent_id: str | None = None,
        level: int = logging.INFO,
        **data: Any,
    ) -> None:
        self.store.append_audit(now, event, data, producer, signal_id, version, intent_id)
        fields = {
            "event": event,
            "at": iso(now),
            "producer": producer,
            "signal_id": signal_id,
            "version": version,
            "intent_id": intent_id,
            **data,
        }
        log.log(level, event, extra={"fields": {k: v for k, v in fields.items() if v is not None}})
