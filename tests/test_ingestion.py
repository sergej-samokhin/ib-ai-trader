"""Transports: file-drop adapter and authenticated HTTP adapter."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helpers_contract import build, example_text, render, split
from ibtrader.audit import Audit
from ibtrader.config import Settings
from ibtrader.gateway import SignalGateway
from ibtrader.ingestion.filedrop import FileDropAdapter
from ibtrader.ingestion.http import HttpAdapter
from ibtrader.notify import LogNotifier
from ibtrader.producers import Producer, ProducerRegistry, ProducerStatus, hash_api_key
from ibtrader.store import Store

NOW = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
MOMO = "momentum-agent"
MACRO = "macro-agent"
TRADE = "trade-bull-call-spread.md"
PUT = "trade-long-put.md"
TRADE_ID = "nvda-momo-2026-10-19-001"


def make_gateway(settings: Settings, producers: list[Producer]) -> tuple[SignalGateway, Store]:
    store = Store(":memory:")
    gateway = SignalGateway(
        settings, ProducerRegistry(producers), store, Audit(store), LogNotifier()
    )
    return gateway, store


# =========================================================================================
# File drop
# =========================================================================================
@dataclass
class Drop:
    adapter: FileDropAdapter
    store: Store
    settings: Settings

    def write(self, producer: str, name: str, text: str, age: float = 60.0) -> Path:
        directory = self.settings.inbox_dir / producer
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        stamp = NOW.timestamp() - age
        os.utime(path, (stamp, stamp))
        return path

    def poll(self, now: datetime = NOW):
        return self.adapter.poll(now)

    def files(self, root: Path, producer: str) -> list[Path]:
        directory = root / producer
        return sorted(directory.iterdir()) if directory.is_dir() else []

    def acks(self, producer: str, stem: str) -> list[dict]:
        path = self.settings.outbox_dir / producer / f"{stem}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def drop(tmp_path):
    settings = Settings(data_dir=tmp_path)
    gateway, store = make_gateway(
        settings, [Producer(id=MOMO, status=ProducerStatus.AUTO), Producer(id=MACRO)]
    )
    adapter = FileDropAdapter(settings, gateway)
    adapter.ensure_dirs([MOMO, MACRO])
    yield Drop(adapter, store, settings)
    store.close()


def test_ensure_dirs_creates_layout(drop):
    s = drop.settings
    for base in (s.inbox_dir, s.outbox_dir, s.processed_dir, s.quarantine_dir):
        assert base.is_dir()
    for pid in (MOMO, MACRO):
        assert (s.inbox_dir / pid).is_dir()
        assert (s.outbox_dir / pid).is_dir()


def test_poll_without_inbox_returns_nothing(tmp_path):
    settings = Settings(data_dir=tmp_path / "missing")
    gateway, store = make_gateway(settings, [Producer(id=MOMO)])
    assert FileDropAdapter(settings, gateway).poll(NOW) == []
    store.close()


def test_accepted_file_moves_to_processed_with_ack(drop):
    src = drop.write(MOMO, "signal.md", build(TRADE))
    results = drop.poll()
    assert [(p, a.status) for p, a in results] == [(src, "accepted")]
    assert not src.exists()
    processed = drop.files(drop.settings.processed_dir, MOMO)
    assert len(processed) == 1
    assert processed[0].name.endswith("-signal.md")
    assert processed[0].read_text(encoding="utf-8") == build(TRADE)
    assert drop.files(drop.settings.quarantine_dir, MOMO) == []
    (ack,) = drop.acks(MOMO, TRADE_ID)
    assert ack["event"] == "ack"
    assert ack["file"] == "signal.md"
    assert (ack["status"], ack["signal_id"], ack["version"]) == ("accepted", TRADE_ID, 1)
    assert ack["errors"] == []
    assert "at" in ack
    row = drop.store.one("SELECT transport, producer FROM raw_signals")
    assert (row["transport"], row["producer"]) == ("file", MOMO)


def test_duplicate_goes_to_processed(drop):
    drop.write(MOMO, "a.md", build(TRADE))
    drop.write(MOMO, "b.md", build(TRADE))
    statuses = [a.status for _, a in drop.poll()]
    assert statuses == ["accepted", "duplicate"]
    assert len(drop.files(drop.settings.processed_dir, MOMO)) == 2
    assert drop.files(drop.settings.quarantine_dir, MOMO) == []
    assert [a["status"] for a in drop.acks(MOMO, TRADE_ID)] == ["accepted", "duplicate"]


def test_invalid_file_goes_to_quarantine_with_field_errors(drop):
    drop.write(MOMO, "bad.md", example_text("invalid", "unknown-key.md"))
    ((_, ack),) = drop.poll()
    assert ack.status == "rejected"
    assert drop.files(drop.settings.processed_dir, MOMO) == []
    (quarantined,) = drop.files(drop.settings.quarantine_dir, MOMO)
    assert quarantined.name.endswith("-bad.md")
    # no signal_id on a validation failure: feedback is named after the file
    (entry,) = drop.acks(MOMO, "bad")
    assert entry["status"] == "rejected"
    assert entry["reason"] == "validation_failed"
    assert entry["errors"] == [{"field": "sizing.leverage", "message": "unknown key"}]


def test_expired_file_goes_to_quarantine(drop):
    drop.write(MOMO, "late.md", build(TRADE))
    late = NOW.replace(hour=21)
    ((_, ack),) = drop.poll(late)
    assert ack.status == "expired"
    assert len(drop.files(drop.settings.quarantine_dir, MOMO)) == 1


def test_spoofed_producer_in_dir_is_rejected(drop):
    drop.write(MACRO, "spoof.md", build(TRADE))  # document says momentum-agent
    ((_, ack),) = drop.poll()
    assert ack.reason == "producer_mismatch"
    assert len(drop.files(drop.settings.quarantine_dir, MACRO)) == 1


def test_fresh_files_are_skipped_until_stable(drop):
    path = drop.write(MOMO, "new.md", build(TRADE), age=0.5)
    assert drop.poll() == []
    assert path.exists()
    assert drop.store.one("SELECT COUNT(*) AS n FROM raw_signals")["n"] == 0
    ((_, ack),) = drop.poll(NOW + timedelta(seconds=2))
    assert ack.status == "accepted"
    assert not path.exists()


def test_file_older_than_one_second_is_processed(drop):
    drop.write(MOMO, "old.md", build(TRADE), age=1.5)
    assert len(drop.poll()) == 1


def test_unknown_producer_directory_quarantined_without_outbox(drop):
    drop.write("ghost-agent", "x.md", build(TRADE, producer="ghost-agent"))
    ((_, ack),) = drop.poll()
    assert ack.reason == "unknown_producer"
    assert len(drop.files(drop.settings.quarantine_dir, "ghost-agent")) == 1
    assert drop.files(drop.settings.processed_dir, "ghost-agent") == []
    assert not (drop.settings.outbox_dir / "ghost-agent").exists()
    assert sorted(p.name for p in drop.settings.outbox_dir.iterdir()) == [MACRO, MOMO]


def test_missing_outbox_dir_of_known_producer_is_created(drop):
    (drop.settings.outbox_dir / MOMO).rmdir()
    drop.write(MOMO, "s.md", build(TRADE))
    drop.poll()
    assert (drop.settings.outbox_dir / MOMO / f"{TRADE_ID}.jsonl").is_file()


def test_symlinked_file_is_skipped(drop, tmp_path):
    secret = tmp_path / "outside.md"
    secret.write_text(build(TRADE), encoding="utf-8")
    link = drop.settings.inbox_dir / MOMO / "link.md"
    link.symlink_to(secret)
    assert drop.poll() == []
    assert link.is_symlink()
    assert secret.exists()
    assert drop.store.one("SELECT COUNT(*) AS n FROM raw_signals")["n"] == 0


def test_symlinked_producer_directory_is_skipped(drop, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "s.md").write_text(build(TRADE), encoding="utf-8")
    os.utime(target / "s.md", (NOW.timestamp() - 60,) * 2)
    (drop.settings.inbox_dir / "linked-agent").symlink_to(target, target_is_directory=True)
    assert drop.poll() == []
    assert (target / "s.md").exists()


def test_non_md_files_and_directories_ignored(drop):
    inbox = drop.settings.inbox_dir / MOMO
    drop.write(MOMO, "notes.txt", "hello")
    drop.write(MOMO, "partial.md.tmp", build(TRADE))
    (inbox / "folder.md").mkdir()
    assert drop.poll() == []
    assert (inbox / "notes.txt").exists()


def test_files_directly_in_inbox_are_ignored(drop):
    stray = drop.settings.inbox_dir / "stray.md"
    stray.write_text(build(TRADE), encoding="utf-8")
    assert drop.poll() == []
    assert stray.exists()


def test_oversized_file_rejected_and_stored_truncated(tmp_path):
    settings = Settings(data_dir=tmp_path, max_signal_bytes=1024)
    gateway, store = make_gateway(settings, [Producer(id=MOMO)])
    adapter = FileDropAdapter(settings, gateway)
    adapter.ensure_dirs([MOMO])
    big = settings.inbox_dir / MOMO / "big.md"
    big.write_text(build(TRADE, body="\n" + "A" * 50_000), encoding="utf-8")
    os.utime(big, (NOW.timestamp() - 60,) * 2)
    ((_, ack),) = adapter.poll(NOW)
    assert ack.reason == "payload_too_large"
    row = store.one("SELECT truncated, size, length(payload) AS n FROM raw_signals")
    assert row["truncated"] == 1
    assert row["n"] <= 1025  # at most limit+1 bytes were read from disk
    assert len(list((settings.quarantine_dir / MOMO).iterdir())) == 1
    store.close()


def test_non_utf8_file_is_handled(drop):
    path = drop.settings.inbox_dir / MOMO / "bin.md"
    path.write_bytes(b"---\n\xff\xfe\x00\n---\n")
    os.utime(path, (NOW.timestamp() - 60,) * 2)
    ((_, ack),) = drop.poll()
    assert ack.status == "rejected"
    assert len(drop.files(drop.settings.quarantine_dir, MOMO)) == 1


def test_multiple_producers_are_processed_in_one_poll(drop):
    drop.write(MOMO, "a.md", build(TRADE))
    drop.write(MACRO, "b.md", build(PUT))
    results = drop.poll()
    assert sorted(p.parent.name for p, _ in results) == [MACRO, MOMO]
    assert all(a.status == "accepted" for _, a in results)


def test_feedback_lines_append(drop):
    drop.adapter.write_feedback(MOMO, {"event": "fill", "signal_id": "abc-123"})
    drop.adapter.write_feedback(MOMO, {"event": "closed", "signal_id": "abc-123"})
    assert [e["event"] for e in drop.acks(MOMO, "abc-123")] == ["fill", "closed"]


def test_feedback_filename_is_sanitised(drop):
    drop.adapter.write_feedback(MOMO, {"event": "x", "signal_id": "../../evil/name"})
    names = [p.name for p in (drop.settings.outbox_dir / MOMO).iterdir()]
    assert names == ["....evilname.jsonl"]
    assert not (drop.settings.data_dir / "evil").exists()


def test_feedback_for_unknown_identity_creates_nothing(drop):
    drop.adapter.write_feedback("nobody", {"event": "x", "signal_id": "abc-123"})
    assert not (drop.settings.outbox_dir / "nobody").exists()


# =========================================================================================
# HTTP
# =========================================================================================
KEY_MOMO = "ibt_test_key_momentum"
KEY_MACRO = "ibt_test_key_macro"


@dataclass
class Server:
    base: str
    store: Store
    settings: Settings

    def call(
        self,
        method: str,
        path: str,
        body: str | bytes | None = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        if key is not None:
            request.add_header("Authorization", f"Bearer {key}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def post(self, text: str, key: str | None = KEY_MOMO, **kwargs):
        return self.call("POST", "/v1/signals", text, key, **kwargs)

    def get(self, path: str, key: str | None = KEY_MOMO):
        return self.call("GET", path, None, key)


def fresh(name: str = TRADE, **overrides) -> str:
    """A signal valid relative to the real clock (the HTTP adapter uses utcnow())."""
    now = datetime.now(UTC).replace(microsecond=0)
    fields = {
        "created_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "valid_until": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    }
    return build(name, **{**fields, **overrides})


@pytest.fixture
def make_server(tmp_path, request):
    def factory(producers: list[Producer] | None = None, **settings_kwargs) -> Server:
        settings = Settings(data_dir=tmp_path, http_port=0, **settings_kwargs)
        gateway, store = make_gateway(
            settings,
            producers
            or [
                Producer(
                    id=MOMO, status=ProducerStatus.AUTO, api_key_sha256=hash_api_key(KEY_MOMO)
                ),
                Producer(id=MACRO, api_key_sha256=hash_api_key(KEY_MACRO)),
            ],
        )
        adapter = HttpAdapter(settings, gateway.registry, gateway, store)
        adapter.start()

        def stop() -> None:
            adapter.stop()
            store.close()

        request.addfinalizer(stop)
        assert adapter.port > 0
        return Server(f"http://127.0.0.1:{adapter.port}", store, settings)

    return factory


@pytest.fixture
def server(make_server) -> Server:
    return make_server()


def test_healthz_needs_no_auth(server):
    assert server.get("/healthz", key=None) == (200, {"ok": True})


def test_post_without_auth_is_401(server):
    code, body = server.post(fresh(), key=None)
    assert code == 401
    assert body["reason"] == "unauthorized"
    assert server.store.one("SELECT COUNT(*) AS n FROM raw_signals")["n"] == 0


def test_post_with_wrong_bearer_is_401(server):
    assert server.post(fresh(), key="ibt_wrong")[0] == 401
    assert server.post(fresh(), key="")[0] == 401


def test_post_with_non_bearer_scheme_is_401(server):
    code, _ = server.post(fresh(), key=None, headers={"Authorization": f"Basic {KEY_MOMO}"})
    assert code == 401


def test_key_of_producer_without_key_cannot_authenticate(make_server):
    srv = make_server([Producer(id=MOMO)])
    assert srv.post(fresh(), key="anything")[0] == 401


def test_post_accepted_is_202(server):
    code, body = server.post(fresh())
    assert code == 202
    assert body == {
        "status": "accepted",
        "signal_id": TRADE_ID,
        "version": 1,
        "reason": None,
        "errors": [],
    }
    row = server.store.one("SELECT transport, producer FROM raw_signals")
    assert (row["transport"], row["producer"]) == ("http", MOMO)
    assert server.store.get_signal(MOMO, TRADE_ID)["status"] == "pending"


def test_post_duplicate_is_200(server):
    assert server.post(fresh())[0] == 202
    code, body = server.post(fresh())
    assert code == 200
    assert body["status"] == "duplicate"


def test_post_invalid_is_422_with_errors(server):
    code, body = server.post(fresh(leverage=3))
    assert code == 422
    assert body["status"] == "rejected"
    assert body["reason"] == "validation_failed"
    assert body["errors"] == [{"field": "leverage", "message": "unknown key"}]


def test_post_garbage_is_422(server):
    code, body = server.post("hello")
    assert code == 422
    assert body["errors"][0]["field"] == "$"


def test_post_expired_is_422(server):
    now = datetime.now(UTC).replace(microsecond=0)
    raw = build(
        TRADE,
        created_at=(now - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        valid_until=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )
    code, body = server.post(raw)
    assert (code, body["status"]) == (422, "expired")


def test_post_spoofed_producer_is_422(server):
    code, body = server.post(fresh(), key=KEY_MACRO)  # document claims momentum-agent
    assert code == 422
    assert body["reason"] == "producer_mismatch"


def test_post_oversized_is_422_and_truncated(make_server):
    srv = make_server(max_signal_bytes=1024)
    code, body = srv.post(fresh(body="\n" + "A" * 5000))
    assert code == 422
    assert body["reason"] == "payload_too_large"
    row = srv.store.one("SELECT truncated, length(payload) AS n, size FROM raw_signals")
    assert row["truncated"] == 1
    assert row["n"] == 1024
    assert row["size"] > 1024


def test_post_rate_limited_is_429(make_server):
    srv = make_server(
        [Producer(id=MOMO, rate_limit_per_hour=2, api_key_sha256=hash_api_key(KEY_MOMO))]
    )
    assert srv.post(fresh(signal_id="rate-limit-001"))[0] == 202
    assert srv.post(fresh(signal_id="rate-limit-002"))[0] == 202
    code, body = srv.post(fresh(signal_id="rate-limit-003"))
    assert code == 429
    assert body["reason"] == "rate_limited"


def test_post_paused_producer_is_422(server):
    server.store.pause_producer(MOMO, "test", datetime.now(UTC))
    code, body = server.post(fresh())
    assert (code, body["reason"]) == (422, "producer_paused")


def test_post_to_unknown_path_is_404(server):
    assert server.call("POST", "/v1/other", "x", KEY_MOMO)[0] == 404
    assert server.get("/nope")[0] == 404


def test_post_body_is_stored_verbatim(server):
    raw = fresh().replace("\n", "\r\n")
    assert server.post(raw)[0] == 202
    assert server.store.one("SELECT payload FROM raw_signals")["payload"] == raw


def test_post_prompt_injection_body_accepted(server):
    data, body = split(example_text("adversarial", "prompt-injection-body.md"))
    now = datetime.now(UTC).replace(microsecond=0)
    data["created_at"] = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    data["valid_until"] = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    code, ack = server.post(render(data, body))
    assert (code, ack["status"]) == (202, "accepted")
    assert "SYSTEM OVERRIDE" in server.store.get_signal(MOMO, "inject-test-001")["body"]


def test_get_history_for_own_signal(server):
    server.post(fresh())
    code, body = server.get(f"/v1/signals/{TRADE_ID}")
    assert code == 200
    assert body["signal_id"] == TRADE_ID
    assert [(h["version"], h["status"], h["reason"]) for h in body["history"]] == [
        (1, "pending", "accepted")
    ]
    assert body["intents"] == []


def test_get_history_shows_rule_supersession(server):
    server.post(fresh("rule-condition.md", version=1))
    server.post(fresh("rule-condition.md", version=2))
    _, body = server.get("/v1/signals/aapl-sma-cross")
    assert [(h["version"], h["status"]) for h in body["history"]] == [
        (1, "active"),
        (2, "active"),
        (1, "superseded"),
    ]


def test_get_other_producers_signal_is_404(server):
    server.post(fresh())
    code, body = server.get(f"/v1/signals/{TRADE_ID}", key=KEY_MACRO)
    assert code == 404
    assert body == {"error": "unknown signal"}


def test_get_unknown_signal_is_404(server):
    assert server.get("/v1/signals/does-not-exist")[0] == 404


def test_get_requires_auth(server):
    server.post(fresh())
    assert server.get(f"/v1/signals/{TRADE_ID}", key=None)[0] == 401
    assert server.get(f"/v1/signals/{TRADE_ID}", key="ibt_wrong")[0] == 401


def test_get_with_invalid_id_pattern_is_404(server):
    assert server.get("/v1/signals/UPPER")[0] == 404
    assert server.get("/v1/signals/a")[0] == 404
    assert server.get("/v1/signals/")[0] == 404


def test_get_lists_intents_for_own_signal(server):
    server.post(fresh())
    now = datetime.now(UTC)
    server.store.insert_intent(
        now,
        id="int_test_1",
        producer=MOMO,
        signal_id=TRADE_ID,
        version=1,
        kind="entry",
        underlying="NVDA",
        strategy="bull_call_spread",
        legs="[]",
        quantity=1,
        status="submitted",
    )
    _, body = server.get(f"/v1/signals/{TRADE_ID}")
    assert [(i["id"], i["status"], i["kind"]) for i in body["intents"]] == [
        ("int_test_1", "submitted", "entry")
    ]
    assert server.get(f"/v1/signals/{TRADE_ID}", key=KEY_MACRO)[0] == 404


def test_server_stops_cleanly(tmp_path):
    settings = Settings(data_dir=tmp_path, http_port=0)
    gateway, store = make_gateway(settings, [Producer(id=MOMO)])
    adapter = HttpAdapter(settings, gateway.registry, gateway, store)
    adapter.start()
    port = adapter.port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        assert response.status == 200
    adapter.stop()
    with pytest.raises(urllib.error.URLError):
        opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2)
    store.close()
