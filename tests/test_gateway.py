"""Signal gateway: validation pipeline, dedupe, versioning, conflict policy and audit trail."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from helpers_contract import DROP, build, example_text
from ibtrader.audit import Audit
from ibtrader.config import Settings
from ibtrader.gateway import SignalGateway
from ibtrader.notify import LogNotifier
from ibtrader.producers import Producer, ProducerRegistry, ProducerStatus
from ibtrader.store import Store

NOW = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
TRADE = "trade-bull-call-spread.md"  # momentum-agent, NVDA bullish, id nvda-momo-2026-10-19-001
PUT = "trade-long-put.md"  # macro-agent, SPY bearish
RULE = "rule-condition.md"  # momentum-agent, AAPL, id aapl-sma-cross
CLOSE = "exit-close.md"  # momentum-agent, targets the trade above
MOMO = "momentum-agent"
MACRO = "macro-agent"
TRADE_ID = "nvda-momo-2026-10-19-001"


def close(**overrides) -> str:
    """The exit example, created before NOW so the future-timestamp check passes."""
    return build(CLOSE, **{"created_at": "2026-10-19T14:00:00Z", **overrides})


@dataclass
class Env:
    gateway: SignalGateway
    store: Store
    notifier: LogNotifier
    settings: Settings

    def submit(self, raw: str, producer: str = MOMO, now: datetime = NOW):
        return self.gateway.submit(raw, producer, "test", now)

    def signal(self, producer: str, signal_id: str, version: int | None = None):
        return self.store.get_signal(producer, signal_id, version)

    def raw(self, raw_id: int):
        return self.store.one("SELECT * FROM raw_signals WHERE id = ?", (raw_id,))

    def events(self) -> list[str]:
        return [r["event"] for r in self.store.query("SELECT event FROM audit_log ORDER BY id")]


@pytest.fixture
def make_env(tmp_path):
    stores: list[Store] = []

    def factory(producers: list[Producer] | None = None, **settings_kwargs) -> Env:
        settings = Settings(data_dir=tmp_path, **settings_kwargs)
        registry = ProducerRegistry(
            producers
            if producers is not None
            else [Producer(id=MOMO, status=ProducerStatus.AUTO), Producer(id=MACRO)]
        )
        store = Store(":memory:")
        stores.append(store)
        notifier = LogNotifier()
        gateway = SignalGateway(settings, registry, store, Audit(store), notifier)
        return Env(gateway, store, notifier, settings)

    yield factory
    for store in stores:
        store.close()


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


# -- happy path ------------------------------------------------------------------------
def test_trade_accepted(env):
    ack = env.submit(build(TRADE))
    assert ack.status == "accepted"
    assert (ack.signal_id, ack.version, ack.reason, ack.errors) == (TRADE_ID, 1, None, [])
    row = env.signal(MOMO, TRADE_ID, 1)
    assert row["status"] == "pending"
    assert row["kind"] == "trade"
    assert row["underlying"] == "NVDA"
    assert row["strategy"] == "bull_call_spread"
    assert json.loads(row["data"])["sizing"]["max_contracts"] == 2
    assert row["raw_id"] == ack.raw_id
    assert env.raw(ack.raw_id)["status"] == "accepted"


def test_rule_accepted_as_active(env):
    assert env.submit(build(RULE)).status == "accepted"
    assert env.signal(MOMO, "aapl-sma-cross")["status"] == "active"


def test_exit_accepted_for_own_target(env):
    env.submit(build(TRADE))
    ack = env.submit(close())
    assert ack.status == "accepted"
    assert env.signal(MOMO, "nvda-momo-2026-10-19-001-exit")["kind"] == "exit"


def test_exit_may_target_a_rule(env):
    env.submit(build(RULE))
    assert env.submit(close(target_signal_id="aapl-sma-cross")).status == "accepted"


def test_ack_as_dict(env):
    ack = env.submit(build(TRADE))
    assert ack.as_dict() == {
        "status": "accepted",
        "signal_id": TRADE_ID,
        "version": 1,
        "reason": None,
        "errors": [],
    }


def test_validation_failure_has_field_errors(env):
    ack = env.submit(example_text("invalid", "unknown-key.md"))
    assert ack.status == "rejected"
    assert ack.reason == "validation_failed"
    assert ack.errors == [{"field": "sizing.leverage", "message": "unknown key"}]
    assert env.store.one("SELECT COUNT(*) AS n FROM signals")["n"] == 0
    stored = env.raw(ack.raw_id)
    assert stored["status"] == "rejected"
    assert json.loads(stored["errors"]) == ack.errors


# -- duplicate / expired ---------------------------------------------------------------
def test_duplicate_same_id_and_version(env):
    first = env.submit(build(TRADE))
    second = env.submit(build(TRADE))
    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert second.reason == "already_processed"
    assert second.raw_id != first.raw_id
    assert env.store.one("SELECT COUNT(*) AS n FROM signals")["n"] == 1


def test_duplicate_even_with_changed_content(env):
    env.submit(build(TRADE))
    assert env.submit(build(TRADE, confidence=0.9)).status == "duplicate"
    assert json.loads(env.signal(MOMO, TRADE_ID)["data"])["confidence"] == 0.62


def test_same_signal_id_from_other_producer_is_independent(env):
    env.submit(build(TRADE))
    ack = env.submit(build(TRADE, producer=MACRO, underlying="NVDA"), MACRO)
    assert ack.status == "accepted"


def test_expired(env):
    ack = env.submit(build(TRADE), now=datetime(2026, 10, 19, 20, 0, tzinfo=UTC))
    assert ack.status == "expired"
    assert ack.reason == "valid_until_in_past"
    assert env.signal(MOMO, TRADE_ID) is None


def test_valid_until_equal_to_now_is_expired(env):
    ack = env.submit(build(TRADE), now=datetime(2026, 10, 19, 19, 55, tzinfo=UTC))
    assert ack.status == "expired"


def test_one_second_before_expiry_accepted(env):
    ack = env.submit(build(TRADE), now=datetime(2026, 10, 19, 19, 54, 59, tzinfo=UTC))
    assert ack.status == "accepted"


# -- identity --------------------------------------------------------------------------
def test_unknown_producer(env):
    ack = env.submit(build(TRADE, producer="ghost-agent"), "ghost-agent")
    assert ack.status == "rejected"
    assert ack.reason == "unknown_producer"
    assert env.raw(ack.raw_id)["producer"] == "ghost-agent"  # still recorded verbatim


def test_producer_mismatch_spoofing(env):
    # transport authenticated macro-agent but the document claims to be momentum-agent
    ack = env.submit(build(TRADE), MACRO)
    assert ack.status == "rejected"
    assert ack.reason == "producer_mismatch"
    assert ack.errors[0]["field"] == "producer"
    assert env.signal(MOMO, TRADE_ID) is None
    assert env.signal(MACRO, TRADE_ID) is None


def test_producer_mismatch_for_exit_cannot_target_via_spoof(env):
    env.submit(build(TRADE))
    ack = env.submit(close(), MACRO)
    assert ack.reason == "producer_mismatch"


def test_created_at_in_future_rejected(env):
    raw = build(TRADE, created_at="2026-10-19T14:40:00Z", valid_until="2026-10-19T19:00:00Z")
    ack = env.submit(raw)
    assert ack.status == "rejected"
    assert ack.reason == "created_in_future"
    assert ack.errors[0]["field"] == "created_at"


def test_created_at_within_clock_skew_accepted(env):
    raw = build(TRADE, created_at="2026-10-19T14:34:00Z", valid_until="2026-10-19T19:00:00Z")
    assert env.submit(raw).status == "accepted"


def test_created_at_exactly_at_skew_limit_accepted(env):
    raw = build(TRADE, created_at="2026-10-19T14:35:00Z", valid_until="2026-10-19T19:00:00Z")
    assert env.submit(raw).status == "accepted"
    raw = build(
        TRADE,
        signal_id="skew-over-001",
        created_at="2026-10-19T14:35:01Z",
        valid_until="2026-10-19T19:00:00Z",
    )
    assert env.submit(raw).reason == "created_in_future"


def test_custom_clock_skew(make_env):
    env = make_env(max_clock_skew_seconds=0)
    raw = build(TRADE, created_at="2026-10-19T14:30:01Z", valid_until="2026-10-19T19:00:00Z")
    assert env.submit(raw).reason == "created_in_future"


# -- paused producers ------------------------------------------------------------------
def test_paused_by_registry(make_env):
    env = make_env([Producer(id=MOMO, status=ProducerStatus.PAUSED)])
    ack = env.submit(build(TRADE))
    assert (ack.status, ack.reason) == ("rejected", "producer_paused")


def test_paused_by_store(env):
    env.store.pause_producer(MOMO, "too many losses", NOW)
    ack = env.submit(build(TRADE))
    assert (ack.status, ack.reason) == ("rejected", "producer_paused")
    assert env.submit(build(PUT, signal_id="macro-ok-001"), MACRO).status == "accepted"


def test_resume_after_pause(env):
    env.store.pause_producer(MOMO, "test", NOW)
    assert env.submit(build(TRADE)).reason == "producer_paused"
    env.store.resume_producer(MOMO)
    assert env.submit(build(TRADE)).status == "accepted"


# -- rate limiting ---------------------------------------------------------------------
def flood(i: int) -> str:
    return build(TRADE, signal_id=f"flood-{i:03d}")


def test_rate_limit_flood(make_env):
    env = make_env([Producer(id=MOMO, rate_limit_per_hour=3)])
    acks = [env.submit(flood(i)) for i in range(4)]
    assert [a.status for a in acks[:3]] == ["accepted"] * 3
    assert acks[3].status == "rejected"
    assert acks[3].reason == "rate_limited"
    assert env.signal(MOMO, "flood-003") is None


def test_rate_limit_counts_rejected_payloads_too(make_env):
    env = make_env([Producer(id=MOMO, rate_limit_per_hour=3)])
    for _ in range(3):
        assert env.submit("garbage").reason == "validation_failed"
    assert env.submit(flood(0)).reason == "rate_limited"


def test_rate_limit_is_per_producer(make_env):
    env = make_env([Producer(id=MOMO, rate_limit_per_hour=1), Producer(id=MACRO)])
    env.submit(flood(0))
    assert env.submit(flood(1)).reason == "rate_limited"
    assert env.submit(build(PUT), MACRO).status == "accepted"


def test_rate_limit_window_slides(make_env):
    env = make_env([Producer(id=MOMO, rate_limit_per_hour=3)])
    for i in range(3):
        env.submit(flood(i))
    assert env.submit(flood(3)).reason == "rate_limited"
    later = NOW + timedelta(hours=1, seconds=1)
    assert env.submit(flood(4), now=later).status == "accepted"


def test_rate_limit_checked_before_paused_and_parse(make_env):
    env = make_env([Producer(id=MOMO, rate_limit_per_hour=1)])
    env.submit("x")
    assert env.submit("y").reason == "rate_limited"


# -- raw payload handling --------------------------------------------------------------
def test_oversized_payload_rejected_and_truncated(make_env):
    env = make_env(max_signal_bytes=1024)
    raw = build(TRADE, body="\n" + "A" * 3000 + "\n")
    ack = env.submit(raw)
    assert ack.status == "rejected"
    assert ack.reason == "payload_too_large"
    row = env.raw(ack.raw_id)
    assert row["truncated"] == 1
    assert row["size"] == len(raw.encode())
    assert len(row["payload"].encode()) == 1024
    assert raw.startswith(row["payload"])
    assert row["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert env.store.one("SELECT COUNT(*) AS n FROM signals")["n"] == 0


def test_payload_exactly_at_limit_accepted(make_env):
    env = make_env(max_signal_bytes=1024)
    base = build(TRADE, body="\n")
    padded = build(TRADE, body="\n" + "x" * (1024 - len(base.encode())) + "\n")
    padded = padded[:-1] if len(padded.encode()) > 1024 else padded
    assert len(padded.encode()) == 1024
    ack = env.submit(padded)
    assert ack.status == "accepted"
    assert env.raw(ack.raw_id)["truncated"] == 0


def test_payload_one_byte_over_limit_rejected(make_env):
    env = make_env(max_signal_bytes=1024)
    base = build(TRADE, body="\n")
    padded = build(TRADE, body="\n" + "x" * (1024 - len(base.encode())) + "\n")
    assert len(padded.encode()) == 1025
    assert env.submit(padded).reason == "payload_too_large"


def test_oversize_multibyte_is_measured_in_bytes(make_env):
    env = make_env(max_signal_bytes=1024)
    raw = build(TRADE, body="\n" + "ä" * 400 + "\n")  # ~800 chars but > 1024 bytes in total
    assert len(raw) < 1024 < len(raw.encode())
    assert env.submit(raw).reason == "payload_too_large"


def test_oversized_from_unknown_producer_still_truncated(make_env):
    env = make_env(max_signal_bytes=1024)
    ack = env.submit("z" * 5000, "ghost-agent")
    assert ack.reason == "payload_too_large"
    assert len(env.raw(ack.raw_id)["payload"]) == 1024


@pytest.mark.parametrize(
    "transform",
    [
        lambda s: s,
        lambda s: s.replace("\n", "\r\n"),
        lambda s: "﻿" + s,
        lambda s: s + "\n\n   \t\n",
        lambda s: s.replace("Rationale", "Begründung äöü ☃"),
    ],
    ids=["plain", "crlf", "bom", "trailing-ws", "unicode"],
)
def test_raw_payload_stored_verbatim(env, transform):
    raw = transform(build(TRADE))
    ack = env.submit(raw)
    assert ack.status == "accepted"
    row = env.raw(ack.raw_id)
    assert row["payload"] == raw
    assert row["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert row["size"] == len(raw.encode())
    assert (row["transport"], row["producer"], row["truncated"]) == ("test", MOMO, 0)


def test_raw_stored_for_every_outcome(env):
    env.submit(build(TRADE))
    env.submit(build(TRADE))
    env.submit("not a signal")
    env.submit(build(TRADE), now=datetime(2026, 10, 19, 20, 0, tzinfo=UTC))
    rows = env.store.query("SELECT status FROM raw_signals ORDER BY id")
    assert [r["status"] for r in rows] == ["accepted", "duplicate", "rejected", "duplicate"]


def test_prompt_injection_body_is_inert_data(env):
    raw = example_text("adversarial", "prompt-injection-body.md")
    ack = env.submit(raw)
    assert ack.status == "accepted"
    row = env.signal(MOMO, "inject-test-001")
    assert row["status"] == "pending"
    assert "SYSTEM OVERRIDE" in row["body"]
    assert "rm -rf" in row["body"]
    data = json.loads(row["data"])
    assert "SYSTEM" not in json.dumps(data)  # never leaks into structured fields
    assert env.raw(ack.raw_id)["payload"] == raw
    assert env.store.get_kv("mode") is None
    assert env.store.query("SELECT * FROM intents") == []
    assert env.store.query("SELECT * FROM positions") == []


def test_body_stored_exactly(env):
    body = "\n## Rationale\nline one\n\n---\nline two\n"
    env.submit(build(TRADE, body=body))
    assert env.signal(MOMO, TRADE_ID)["body"] == body


# -- rule versioning -------------------------------------------------------------------
def rule(version: int, **overrides) -> str:
    return build(RULE, version=version, **overrides)


def test_rule_v2_supersedes_v1(env):
    assert env.submit(rule(1)).status == "accepted"
    ack = env.submit(rule(2))
    assert ack.status == "accepted"
    assert env.signal(MOMO, "aapl-sma-cross", 1)["status"] == "superseded"
    assert env.signal(MOMO, "aapl-sma-cross", 2)["status"] == "active"
    history = [
        (r["version"], r["status"]) for r in env.store.signal_history(MOMO, "aapl-sma-cross")
    ]
    assert history == [(1, "active"), (2, "active"), (1, "superseded")]


def test_rule_v3_supersedes_only_v2(env):
    for v in (1, 2, 3):
        env.submit(rule(v))
    statuses = {v: env.signal(MOMO, "aapl-sma-cross", v)["status"] for v in (1, 2, 3)}
    assert statuses == {1: "superseded", 2: "superseded", 3: "active"}


def test_rule_version_gap_is_allowed(env):
    env.submit(rule(1))
    assert env.submit(rule(5)).status == "accepted"
    assert env.signal(MOMO, "aapl-sma-cross", 1)["status"] == "superseded"


def test_stale_rule_version_rejected(env):
    assert env.submit(rule(2)).status == "accepted"
    ack = env.submit(rule(1))
    assert ack.status == "rejected"
    assert ack.reason == "stale_version"
    assert ack.errors[0]["field"] == "version"
    assert env.signal(MOMO, "aapl-sma-cross", 1) is None
    assert env.signal(MOMO, "aapl-sma-cross", 2)["status"] == "active"


def test_resubmitting_v1_after_v2_is_duplicate(env):
    env.submit(rule(1))
    env.submit(rule(2))
    assert env.submit(rule(1)).status == "duplicate"


def test_rule_update_does_not_resurrect_cancelled_version(env):
    env.submit(rule(1))
    env.store.set_signal_status(MOMO, "aapl-sma-cross", 1, "cancelled", "test", NOW)
    assert env.submit(rule(2)).status == "accepted"
    assert env.signal(MOMO, "aapl-sma-cross", 1)["status"] == "cancelled"


def test_rule_versions_are_per_producer(env):
    env.submit(rule(2))
    other = build(RULE, version=1, producer=MACRO)
    assert env.submit(other, MACRO).status == "accepted"
    assert env.signal(MOMO, "aapl-sma-cross", 2)["status"] == "active"


# -- immutability and kind reuse -------------------------------------------------------
def test_trade_version_2_rejected_by_contract(env):
    env.submit(build(TRADE))
    ack = env.submit(build(TRADE, version=2))
    assert (ack.status, ack.reason) == ("rejected", "validation_failed")
    assert ack.errors == [
        {"field": "version", "message": "trade signals are immutable; version must be 1"}
    ]
    assert env.signal(MOMO, TRADE_ID)["version"] == 1
    assert env.signal(MOMO, TRADE_ID)["status"] == "pending"


def test_exit_version_2_rejected_by_contract(env):
    env.submit(build(TRADE))
    env.submit(close())
    ack = env.submit(close(version=2))
    assert ack.reason == "validation_failed"


def test_signal_id_reused_trade_then_rule(env):
    env.submit(build(TRADE))
    ack = env.submit(build(RULE, signal_id=TRADE_ID, version=2))
    assert (ack.status, ack.reason) == ("rejected", "signal_id_reused")
    assert ack.errors[0]["field"] == "signal_id"
    assert env.signal(MOMO, TRADE_ID)["kind"] == "trade"
    assert env.signal(MOMO, TRADE_ID)["status"] == "pending"


def test_signal_id_reused_rule_then_trade(env):
    env.submit(build(RULE, version=2))
    ack = env.submit(build(TRADE, signal_id="aapl-sma-cross"))
    assert ack.reason == "signal_id_reused"
    assert env.signal(MOMO, "aapl-sma-cross")["kind"] == "rule"


def test_same_id_and_version_of_another_kind_is_a_duplicate(env):
    # dedupe key is (producer, signal_id, version); it is checked before the kind
    env.submit(build(TRADE))
    ack = env.submit(close(signal_id=TRADE_ID, target_signal_id=TRADE_ID))
    assert ack.status == "duplicate"
    assert env.signal(MOMO, TRADE_ID)["kind"] == "trade"


# -- exits -----------------------------------------------------------------------------
def test_exit_unknown_target(env):
    ack = env.submit(close())
    assert (ack.status, ack.reason) == ("rejected", "unknown_target")
    assert ack.errors[0]["field"] == "target_signal_id"


def test_exit_cannot_target_other_producers_signal(env):
    env.submit(build(TRADE))  # momentum-agent's
    foreign = close(producer=MACRO, signal_id="macro-exit-001")
    ack = env.submit(foreign, MACRO)
    assert (ack.status, ack.reason) == ("rejected", "unknown_target")
    assert env.signal(MACRO, "macro-exit-001") is None


def test_exit_cannot_target_another_exit(env):
    env.submit(build(TRADE))
    env.submit(close())
    ack = env.submit(
        close(signal_id="second-exit-001", target_signal_id="nvda-momo-2026-10-19-001-exit")
    )
    assert ack.reason == "unknown_target"


def test_exit_reduce_accepted(env):
    env.submit(build(RULE))
    reduce_ = build("exit-reduce.md", created_at="2026-10-19T14:00:00Z")
    ack = env.submit(reduce_)
    assert ack.status == "accepted"
    assert json.loads(env.signal(MOMO, "aapl-sma-cross-reduce-001")["data"])["reduce_pct"] == 50


def test_exit_has_no_conflict_check(env):
    env.submit(build(TRADE))
    env.submit(build(PUT, underlying="NVDA", producer=MACRO), MACRO)  # conflict, rejected
    assert env.submit(close()).status == "accepted"


# -- conflict policy -------------------------------------------------------------------
def bearish_nvda(producer: str = MACRO, signal_id: str = "macro-nvda-put-001") -> str:
    return build(PUT, underlying="NVDA", producer=producer, signal_id=signal_id)


def test_conflict_opposite_direction_other_producer(env):
    assert env.submit(build(TRADE)).status == "accepted"
    ack = env.submit(bearish_nvda(), MACRO)
    assert (ack.status, ack.reason) == ("rejected", "conflict")
    assert ack.errors[0]["field"] == "underlying"
    assert "momentum-agent/nvda-momo-2026-10-19-001" in ack.errors[0]["message"]
    assert env.signal(MACRO, "macro-nvda-put-001") is None
    assert len(env.notifier.sent) == 1
    subject, text, level = env.notifier.sent[0]
    assert subject == "Signal conflict"
    assert "macro-agent/macro-nvda-put-001" in text
    assert level == "warning"


def test_conflict_applies_to_active_rules(env):
    env.submit(build(RULE, underlying="NVDA"))
    assert env.submit(bearish_nvda(), MACRO).reason == "conflict"


def test_same_producer_opposite_direction_allowed(env):
    env.submit(build(TRADE))
    ack = env.submit(bearish_nvda(MOMO, "momo-nvda-put-001"))
    assert ack.status == "accepted"
    assert env.notifier.sent == []


def test_same_direction_other_producer_allowed(env):
    env.submit(build(TRADE))
    bullish = build(TRADE, producer=MACRO, signal_id="macro-nvda-call-001")
    assert env.submit(bullish, MACRO).status == "accepted"


def test_other_underlying_does_not_conflict(env):
    env.submit(build(TRADE))
    assert env.submit(build(PUT), MACRO).status == "accepted"  # SPY put


def test_conflict_with_open_position_of_other_producer(env):
    env.store.insert_position(
        NOW,
        intent_id="int_1",
        producer=MOMO,
        signal_id="old-nvda-001",
        version=1,
        underlying="NVDA",
        strategy="long_call",
        legs="[]",
        quantity=1,
        entry_price=2.5,
        exit_rules="{}",
    )
    ack = env.submit(bearish_nvda(), MACRO)
    assert (ack.status, ack.reason) == ("rejected", "conflict")
    assert "position" in ack.errors[0]["message"]
    assert len(env.notifier.sent) == 1


def test_open_position_of_same_producer_does_not_conflict(env):
    env.store.insert_position(
        NOW,
        intent_id="int_1",
        producer=MACRO,
        signal_id="old-nvda-001",
        version=1,
        underlying="NVDA",
        strategy="long_call",
        legs="[]",
        quantity=1,
        entry_price=2.5,
        exit_rules="{}",
    )
    assert env.submit(bearish_nvda(), MACRO).status == "accepted"


def test_closed_position_does_not_conflict(env):
    pos = env.store.insert_position(
        NOW,
        intent_id="int_1",
        producer=MOMO,
        signal_id="old-nvda-001",
        version=1,
        underlying="NVDA",
        strategy="long_call",
        legs="[]",
        quantity=1,
        entry_price=2.5,
        exit_rules="{}",
    )
    env.store.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (pos,))
    assert env.submit(bearish_nvda(), MACRO).status == "accepted"


def test_inactive_signal_does_not_conflict(env):
    env.submit(build(TRADE))
    env.store.set_signal_status(MOMO, TRADE_ID, 1, "cancelled", "test", NOW)
    assert env.submit(bearish_nvda(), MACRO).status == "accepted"


def test_conflict_is_symmetric(env):
    assert env.submit(bearish_nvda(), MACRO).status == "accepted"
    ack = env.submit(build(TRADE))
    assert ack.reason == "conflict"


def test_conflict_checked_after_validation_and_duplicates(env):
    env.submit(build(TRADE))
    env.submit(bearish_nvda(), MACRO)  # conflict, rejected
    again = env.submit(bearish_nvda(), MACRO)
    assert again.reason == "conflict"  # not stored, so it is not a duplicate


# -- audit trail -----------------------------------------------------------------------
def test_audit_rows_written(env):
    ack = env.submit(build(TRADE))
    rows = env.store.query("SELECT * FROM audit_log ORDER BY id")
    assert [r["event"] for r in rows] == ["signal_received", "signal_accepted"]
    received, accepted = rows
    assert received["producer"] == MOMO
    assert json.loads(received["data"])["raw_id"] == ack.raw_id
    assert json.loads(received["data"])["transport"] == "test"
    assert (accepted["signal_id"], accepted["version"]) == (TRADE_ID, 1)
    assert accepted["ts"] == "2026-10-19T14:30:00+00:00"


@pytest.mark.parametrize(
    ("raw", "producer", "event"),
    [
        ("junk", MOMO, "signal_rejected"),
        (build(TRADE, producer="ghost-agent"), "ghost-agent", "signal_rejected"),
    ],
)
def test_audit_for_rejections(env, raw, producer, event):
    ack = env.submit(raw, producer)
    assert env.events() == ["signal_received", event]
    last = env.store.one("SELECT data FROM audit_log ORDER BY id DESC LIMIT 1")
    assert json.loads(last["data"])["reason"] == ack.reason


def test_audit_for_duplicate_and_expired(env):
    env.submit(build(TRADE))
    env.submit(build(TRADE))
    env.submit(build(PUT), MACRO, now=datetime(2026, 10, 19, 21, 0, tzinfo=UTC))
    assert env.events() == [
        "signal_received",
        "signal_accepted",
        "signal_received",
        "signal_duplicate",
        "signal_received",
        "signal_expired",
    ]


def test_audit_contains_validation_errors(env):
    env.submit(build(TRADE, leverage=1))
    last = env.store.one("SELECT data FROM audit_log ORDER BY id DESC LIMIT 1")
    assert json.loads(last["data"])["errors"] == [{"field": "leverage", "message": "unknown key"}]


def test_audit_log_is_append_only(env):
    env.submit(build(TRADE))
    before = env.store.query("SELECT * FROM audit_log ORDER BY id")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.store.execute("UPDATE audit_log SET event = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.store.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.store.execute("DELETE FROM audit_log WHERE id = 1")
    after = env.store.query("SELECT * FROM audit_log ORDER BY id")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_audit_log_update_error_is_integrity_error(env):
    env.submit(build(TRADE))
    with pytest.raises(sqlite3.IntegrityError):
        env.store.execute("UPDATE audit_log SET data = '{}' WHERE id = 1")


def test_dropping_a_key_helper_sanity():
    # guards the test helper itself: DROP really removes keys
    assert "valid_until:" not in build(TRADE, valid_until=DROP)


@pytest.mark.parametrize(
    ("mode", "port", "unsafe"),
    [("paper", 4003, True), ("paper", 4004, False), ("live", 4004, True), ("live", 4003, False)],
)
def test_mode_safety_knows_compose_relay_ports(mode, port, unsafe):
    from ibtrader.config import Settings, check_mode_safety

    problems = check_mode_safety(
        Settings(mode=mode, ib_port=port, ib_account="U123" if mode == "live" else "")
    )
    assert bool(problems) is unsafe
