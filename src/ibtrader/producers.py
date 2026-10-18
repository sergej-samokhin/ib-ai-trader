"""Producer registry: identities, credentials, trust status and budgets of upstream agents."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

PRODUCER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,63}$"


class ProducerStatus(StrEnum):
    PAPER_ONLY = "paper_only"  # in live mode: simulated (shadow) only
    MANUAL = "manual"  # every entry needs human approval
    AUTO = "auto"  # entries execute automatically within global limits
    PAUSED = "paused"  # all new signals rejected


class Producer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=PRODUCER_ID_PATTERN)
    status: ProducerStatus = ProducerStatus.PAPER_ONLY
    budget_usd: float = Field(500.0, ge=0)
    api_key_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    rate_limit_per_hour: int = Field(60, ge=1)
    description: str = ""


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return "ibt_" + secrets.token_urlsafe(32)


class ProducerRegistry:
    def __init__(self, producers: list[Producer]):
        self._by_id = {p.id: p for p in producers}
        if len(self._by_id) != len(producers):
            raise ValueError("duplicate producer id in registry")

    @classmethod
    def load(cls, path: Path) -> ProducerRegistry:
        if not path.is_file():
            return cls([])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = data.get("producers", {}) or {}
        return cls([Producer(id=pid, **(cfg or {})) for pid, cfg in items.items()])

    def get(self, producer_id: str) -> Producer | None:
        return self._by_id.get(producer_id)

    def all(self) -> list[Producer]:
        return list(self._by_id.values())

    def authenticate_key(self, api_key: str) -> Producer | None:
        """Constant-time match of a bearer key against all registered key hashes."""
        digest = hash_api_key(api_key)
        found: Producer | None = None
        for producer in self._by_id.values():
            if producer.api_key_sha256 and hmac.compare_digest(producer.api_key_sha256, digest):
                found = producer
        return found
