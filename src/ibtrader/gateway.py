"""Signal Gateway: the single entry point for every transport.

Authenticate -> rate limit -> parse/validate -> dedupe -> TTL -> kind rules -> conflict policy
-> store. Every raw payload is stored verbatim, whatever the outcome.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from ibtrader.audit import Audit
from ibtrader.config import Settings
from ibtrader.contract import ExitSignal, FieldError, RuleSignal, SignalParseError, parse_signal
from ibtrader.contract.models import Strategy
from ibtrader.notify import Notifier
from ibtrader.producers import ProducerRegistry, ProducerStatus
from ibtrader.store import ACTIVE_SIGNAL_STATUSES, Store

AckStatus = Literal["accepted", "rejected", "duplicate", "expired"]


@dataclass
class Ack:
    status: AckStatus
    signal_id: str | None = None
    version: int | None = None
    reason: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    raw_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "signal_id": self.signal_id,
            "version": self.version,
            "reason": self.reason,
            "errors": self.errors,
        }


class SignalGateway:
    def __init__(
        self,
        settings: Settings,
        registry: ProducerRegistry,
        store: Store,
        audit: Audit,
        notifier: Notifier,
    ):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.audit = audit
        self.notifier = notifier

    def submit(self, raw: str, producer_id: str, transport: str, now: datetime) -> Ack:
        """`producer_id` is the identity proven by the transport (dir, API key...)."""
        encoded = raw.encode("utf-8", errors="replace")
        too_large = len(encoded) > self.settings.max_signal_bytes
        stored = (
            raw
            if not too_large
            else encoded[: self.settings.max_signal_bytes].decode("utf-8", errors="replace")
        )
        raw_id = self.store.record_raw(
            received_at=now,
            transport=transport,
            producer=producer_id,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size=len(encoded),
            payload=stored,
            truncated=too_large,
        )
        self.audit(
            "signal_received",
            now,
            producer=producer_id,
            transport=transport,
            raw_id=raw_id,
            size=len(encoded),
        )
        ack = self._evaluate(raw, producer_id, now, too_large, raw_id)
        ack.raw_id = raw_id
        self.store.set_raw_status(raw_id, ack.status, ack.errors)
        self.audit(
            f"signal_{ack.status}",
            now,
            producer=producer_id,
            signal_id=ack.signal_id,
            version=ack.version,
            raw_id=raw_id,
            reason=ack.reason,
            errors=ack.errors or None,
        )
        return ack

    # -----------------------------------------------------------------------------
    def _reject(self, reason: str, errors: list[FieldError] | None = None, sig=None) -> Ack:
        return Ack(
            "rejected",
            getattr(sig, "signal_id", None),
            getattr(sig, "version", None),
            reason,
            [e.as_dict() for e in errors or []],
        )

    def _evaluate(
        self, raw: str, producer_id: str, now: datetime, too_large: bool, raw_id: int
    ) -> Ack:
        if too_large:
            return self._reject(
                "payload_too_large",
                [FieldError("$", f"payload exceeds {self.settings.max_signal_bytes} bytes")],
            )
        producer = self.registry.get(producer_id)
        if producer is None:
            return self._reject("unknown_producer")
        window_start = now - timedelta(hours=1)
        # The current payload is already recorded, hence ">".
        if self.store.count_raw_since(producer_id, window_start) > producer.rate_limit_per_hour:
            return self._reject("rate_limited")
        if producer.status == ProducerStatus.PAUSED or self.store.is_producer_paused(producer_id):
            return self._reject("producer_paused")

        try:
            parsed = parse_signal(raw)
        except SignalParseError as exc:
            return self._reject("validation_failed", exc.errors)
        sig = parsed.signal

        if sig.producer != producer_id:
            return self._reject(
                "producer_mismatch",
                [FieldError("producer", "does not match the authenticated producer identity")],
                sig,
            )
        if sig.created_at > now + timedelta(seconds=self.settings.max_clock_skew_seconds):
            return self._reject(
                "created_in_future", [FieldError("created_at", "is in the future")], sig
            )
        if self.store.get_signal(producer_id, sig.signal_id, sig.version) is not None:
            return Ack("duplicate", sig.signal_id, sig.version, "already_processed")
        if sig.valid_until <= now:
            return Ack("expired", sig.signal_id, sig.version, "valid_until_in_past")

        latest = self.store.get_signal(producer_id, sig.signal_id)
        if latest is not None:
            if latest["kind"] != sig.kind:
                return self._reject(
                    "signal_id_reused",
                    [FieldError("signal_id", "already used for another kind")],
                    sig,
                )
            if sig.kind != "rule":
                return self._reject(
                    "immutable", [FieldError("version", f"{sig.kind} signals are immutable")], sig
                )
            if latest["version"] > sig.version:
                return self._reject(
                    "stale_version",
                    [FieldError("version", f"newer version {latest['version']} already accepted")],
                    sig,
                )

        if isinstance(sig, ExitSignal):
            target = self.store.get_signal(producer_id, sig.target_signal_id)
            if target is None or target["kind"] == "exit":
                return self._reject(
                    "unknown_target",
                    [FieldError("target_signal_id", "no trade/rule signal with this id from you")],
                    sig,
                )
        else:
            conflict = self._conflict(producer_id, sig.underlying, Strategy(sig.strategy))
            if conflict:
                self.notifier.send(
                    "Signal conflict",
                    f"{producer_id}/{sig.signal_id} v{sig.version} rejected: {conflict}",
                    "warning",
                )
                return self._reject("conflict", [FieldError("underlying", conflict)], sig)

        status = "active" if isinstance(sig, RuleSignal) else "pending"
        self.store.insert_signal(
            producer=producer_id,
            signal_id=sig.signal_id,
            version=sig.version,
            kind=sig.kind,
            underlying=getattr(sig, "underlying", None),
            strategy=str(sig.strategy) if hasattr(sig, "strategy") else None,
            status=status,
            valid_until=sig.valid_until,
            data=sig.model_dump(mode="json"),
            body=parsed.body,
            raw_id=raw_id,
            received_at=now,
        )
        if latest is not None and latest["status"] in ACTIVE_SIGNAL_STATUSES:
            self.store.set_signal_status(
                producer_id,
                sig.signal_id,
                latest["version"],
                "superseded",
                f"replaced by version {sig.version}",
                now,
            )
        return Ack("accepted", sig.signal_id, sig.version)

    def _conflict(self, producer_id: str, underlying: str, strategy: Strategy) -> str | None:
        """Default conflict policy: opposite direction on the same underlying from another
        producer (active signal or open position) -> reject the newer signal."""
        mine = strategy.direction
        for row in self.store.signals_with_status(*ACTIVE_SIGNAL_STATUSES):
            if (
                row["producer"] != producer_id
                and row["underlying"] == underlying
                and row["strategy"]
                and Strategy(row["strategy"]).direction != mine
            ):
                return (
                    f"opposite-direction signal {row['producer']}/{row['signal_id']} is active "
                    f"on {underlying}"
                )
        for pos in self.store.open_positions():
            if (
                pos["producer"] != producer_id
                and pos["underlying"] == underlying
                and Strategy(pos["strategy"]).direction != mine
            ):
                return f"opposite-direction position {pos['id']} from {pos['producer']} is open"
        return None
