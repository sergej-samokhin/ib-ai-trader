"""WSB radar: ticker extraction, sentiment, analytics, features, providers, store and service."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ibtrader.radar import features
from ibtrader.radar.analytics import analyze, count_mentions, extract_tickers, sentiment
from ibtrader.radar.analytics import merge_catalog as _merge
from ibtrader.radar.catalog import load_catalog
from ibtrader.radar.config import RadarSettings, load_radar_settings
from ibtrader.radar.providers import ProviderError, author_hash, fetch, normalize
from ibtrader.radar.service import Radar
from ibtrader.radar.store import RadarStore

# Monday 2026-10-19 18:00 UTC = 14:00 New York (before the 15:00 daily cutoff).
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
T = NOW.timestamp()
CATALOG = {"BB": {"name": "BlackBerry"}, "AI": {"name": "C3.ai"}, "IT": {"name": "Gartner"}}


def item(i: int, text: str, age: float = 60, kind: str = "post", author: str | None = "a", **kw):
    data = {
        "id": f"t3_x{i}",
        "kind": kind,
        "title": "",
        "text": text,
        "url": "",
        "created_utc": T - age,
        "score": kw.pop("score", None),
    }
    if author:
        data["author_hash"] = author
    data.update(kw)
    return data


# ------------------------------------------------------------------------------ extraction
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("NVDA to the moon, also TSLA", ["NVDA", "TSLA"]),
        ("$AI calls but AI hype is AI", ["AI"]),  # cashtag wins over slang
        ("IT is fine, ON it", []),  # ambiguous words need a cashtag
        ("$IT earnings", ["IT"]),
        ("BB is back", []),  # two letters need a cashtag
        ("$BB is back", ["BB"]),
        ("MU looks cheap", ["MU"]),  # curated short symbol
        ("nvda lowercase", []),
        ("see https://x.com/NVDA/status", []),  # URLs are ignored
        ("[deleted]", []),
        ("$ZZZZ unknown", []),
    ],
)
def test_extract_tickers(text, expected):
    assert extract_tickers(text, CATALOG) == expected


@pytest.mark.parametrize(
    ("text", "score", "uncertain"),
    [
        ("NVDA bullish, loading calls", 1, False),
        ("NVDA is not bullish", -1, False),
        ("NVDA puts", -1, False),
        ("NVDA to the moon /s", 0, True),
        ("NVDA calls and TSLA puts", 0, True),  # two tickers in one clause
        ("NVDA calls. NVDA puts.", 0, True),  # mixed evidence
        ("NVDA earnings tomorrow", 0, False),
    ],
)
def test_sentiment(text, score, uncertain):
    assert sentiment(text, "NVDA", _merge(None)) == (score, uncertain)


# ------------------------------------------------------------------------------ analytics
def test_analyze_counts_windows_authors_and_rank():
    items = [
        item(1, "NVDA bullish", age=100, author="a"),
        item(2, "NVDA calls", age=200, author="b", kind="comment"),
        item(3, "NVDA bullish", age=4000, author="a"),
        item(4, "TSLA puts", age=300, author=None),
        item(5, "NVDA old", age=90000),  # outside 24 h
        item(1, "NVDA bullish", age=100, author="a"),  # duplicate id counts once
    ]
    rows = {r["ticker"]: r for r in analyze(items, T, {}, None)}
    nvda = rows["NVDA"]
    assert (nvda["mentions_24h"], nvda["mentions_1h"], nvda["previous_hour"]) == (3, 2, 1)
    assert (nvda["posts"], nvda["comments"], nvda["bullish"]) == (2, 1, 3)
    assert nvda["unique_authors"] == 2 and nvda["breadth"] == pytest.approx(0.667)
    assert nvda["growth"] == 100.0 and nvda["rank"] == 1 and nvda["is_new"]
    assert nvda["sparkline"][23] == 2 and nvda["sparkline"][22] == 1 and sum(nvda["sparkline"]) == 3
    assert rows["TSLA"]["unique_authors"] is None and rows["TSLA"]["breadth"] is None
    assert rows["TSLA"]["sentiment_label"] == "bearish"


def test_analyze_growth_needs_baseline_and_deleted_items_vanish():
    items = [item(1, "NVDA", age=100), item(2, "NVDA", age=4000), item(1, "[removed]", age=100)]
    rows = analyze(items, T, {}, None, baseline_ready=False)
    assert rows[0]["mentions_24h"] == 1 and rows[0]["growth"] is None


def test_analyze_flags_etfs_and_keeps_first_seen():
    first = {"SPY": T - 3 * 86400}
    rows = {r["ticker"]: r for r in analyze([item(1, "SPY and NVDA")], T, first, None)}
    assert rows["SPY"]["is_etf"] and not rows["SPY"]["is_new"]
    assert not rows["NVDA"]["is_etf"] and first["NVDA"] == T


def test_evidence_is_capped_and_sorted_by_score():
    items = [item(i, "NVDA", score=i) for i in range(8)]
    evidence = analyze(items, T, {}, None)[0]["evidence"]
    assert [e["score"] for e in evidence] == [7, 6, 5, 4, 3]


def test_count_mentions_window():
    items = [item(1, "NVDA", age=86400 + 10), item(2, "NVDA", age=100)]
    assert count_mentions(items, T - 2 * 86400, T - 86400, None) == {"NVDA": 1}


# ------------------------------------------------------------------------------ features
def test_robust_z_needs_history_and_floors_mad():
    assert features.robust_z(10, [1.0] * 6) is None
    assert features.robust_z(10, [0.0] * 30) == 10.0  # MAD 0 -> denominator 1
    baseline = [10.0, 12.0, 8.0, 11.0, 9.0, 10.0, 10.0]
    assert features.robust_z(13, baseline) == pytest.approx(2.02, abs=0.01)


def test_enrich_accel_and_history():
    rows = [{"ticker": "NVDA", "mentions_24h": 30}, {"ticker": "TSLA", "mentions_24h": 5}]
    days = [f"2026-10-{d:02d}" for d in range(1, 11)]
    features.enrich(rows, {"NVDA": {d: 5 for d in days}}, days, {"NVDA": 10})
    assert rows[0]["attention_accel"] == 3.0 and rows[0]["history_days"] == 10
    assert rows[0]["attention_z"] == 25.0
    assert rows[1]["attention_accel"] is None and rows[1]["attention_z"] == 5.0
    features.enrich(rows, {}, [], None)
    assert rows[0]["attention_accel"] is None and rows[0]["attention_z"] is None


# ------------------------------------------------------------------------------ providers
def test_normalize_rejects_foreign_urls_and_hashes_authors():
    row = {
        "id": "abc",
        "created_utc": T,
        "subreddit": "wallstreetbets",
        "title": "NVDA",
        "url": "https://evil.example/x",
        "author": "SomeUser",
        "score": 5,
    }
    out = normalize(row, "post", "wallstreetbets")
    assert out["id"] == "t3_abc" and out["url"] == "https://www.reddit.com/r/wallstreetbets/"
    assert out["author_hash"] == author_hash("someuser") and "SomeUser" not in json.dumps(out)
    assert normalize(dict(row, subreddit="stocks"), "post", "wallstreetbets") is None
    assert normalize(dict(row, id="bad id"), "post", "wallstreetbets") is None
    assert author_hash("[deleted]") is None


def _arctic_requester(posts: list[dict], comments: list[dict], calls: list[str]):
    def request(url, **_):
        calls.append(url)
        return {"data": posts if "/posts/" in url else comments}

    return request


def test_arctic_shift_maps_rows_and_drops_out_of_window():
    calls: list[str] = []
    posts = [
        {
            "id": "p1",
            "created_utc": T - 60,
            "subreddit": "wallstreetbets",
            "title": "$NVDA",
            "selftext": "calls",
            "author": "u1",
            "score": 999,
        },
        {"id": "p2", "created_utc": T - 90000, "subreddit": "wallstreetbets", "title": "old"},
        {"id": "p3", "created_utc": T - 60, "subreddit": "stocks", "title": "other sub"},
    ]
    comments = [
        {
            "id": "c1",
            "created_utc": T - 30,
            "subreddit": "wallstreetbets",
            "body": "TSLA puts",
            "link_id": "t3_p1",
            "author": "u2",
        }
    ]
    out = fetch(RadarSettings(), T, _arctic_requester(posts, comments, calls))
    by_id = {i["id"]: i for i in out["items"]}
    assert set(by_id) == {"t3_p1", "t1_c1"}
    assert by_id["t3_p1"]["score"] is None  # provisional archive votes are not used
    assert by_id["t1_c1"]["url"].endswith("/comments/p1/_/c1/")
    assert out["coverage"]["skipped"] == 2 and not out["coverage"]["partial"]
    assert len(calls) == 2 and all(u.startswith("https://arctic-shift") for u in calls)


def test_arctic_shift_incremental_window_and_future_guard():
    calls: list[str] = []
    fetch(RadarSettings(), T, _arctic_requester([], [], calls), since=T - 3600)
    assert f"after={int(T - 3600 - 1200)}" in calls[0]
    with pytest.raises(ProviderError):
        fetch(RadarSettings(), T, _arctic_requester([], [], []), since=T + 100)


def test_provider_errors_are_not_swallowed():
    def broken(url, **_):
        return {"unexpected": True}

    with pytest.raises(ProviderError):
        fetch(RadarSettings(), T, broken)
    with pytest.raises(ProviderError, match="REDDIT_CLIENT_ID"):
        fetch(RadarSettings(provider="reddit"), T, broken)


def test_reddit_oauth_listing():
    calls: list[tuple[str, dict]] = []

    def request(url, headers=None, data=None, **_):
        calls.append((url, headers or {}))
        if url.endswith("access_token"):
            return {"access_token": "tok"}
        child = {
            "kind": "t3",
            "data": {
                "name": "t3_q1",
                "created_utc": T - 10,
                "subreddit": "wallstreetbets",
                "title": "GME",
                "permalink": "/r/wallstreetbets/comments/q1/",
                "score": 7,
            },
        }
        return {"data": {"children": [child], "after": None}}

    settings = RadarSettings(
        provider="reddit",
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_refresh_token="refresh",
    )
    out = fetch(settings, T, request)
    assert [i["id"] for i in out["items"]] == ["t3_q1"] and out["items"][0]["score"] == 7
    assert calls[1][1]["Authorization"] == "Bearer tok"


def test_rss_feed_parsing_and_dtd_refusal():
    feed = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        b"<id>t3_r1</id><title>$PLTR breakout</title><published>2026-10-19T17:59:00+00:00"
        b"</published><content>&lt;p&gt;bullish&lt;/p&gt; submitted by /u/x</content>"
        b'<link href="https://www.reddit.com/r/wallstreetbets/comments/r1/"/></entry></feed>'
    )
    out = fetch(RadarSettings(provider="rss"), T, lambda url, **_: feed)
    assert out["items"][0]["text"] == "bullish" and out["items"][0]["kind"] == "post"
    evil = b'<!DOCTYPE x [<!ENTITY a "b">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
    with pytest.raises(ProviderError):
        fetch(RadarSettings(provider="rss"), T, lambda url, **_: evil)


def test_demo_is_deterministic_per_slot():
    a = fetch(RadarSettings(provider="demo"), T)
    b = fetch(RadarSettings(provider="demo"), T + 1)
    assert [i["id"] for i in a["items"]] == [i["id"] for i in b["items"]]
    assert a["coverage"]["synthetic"]


# ------------------------------------------------------------------------------ config/catalog
def test_radar_settings_from_env():
    s = load_radar_settings(
        {
            "RADAR_ENABLED": "true",
            "RADAR_PROVIDER": " Reddit ",
            "REDDIT_CLIENT_ID": "x",
            "MAX_POSITION_USD": "99999",
        },
        dotenv=None,
    )
    assert s.enabled and s.provider == "reddit" and s.reddit_client_id == "x"
    with pytest.raises(ValueError):
        load_radar_settings({"RADAR_INTERVAL_SECONDS": "60"}, dotenv=None)


def test_catalog_refresh_failure_keeps_cache(tmp_path):
    body = (
        "Symbol|Security Name|ETF|Test Issue\n"
        + "".join(f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}|Name {i}|N|N\n" for i in range(150))
        + "QQQX|Fund|Y|N\nTEST|Test|N|Y\n"
    )
    catalog, fetched = load_catalog(tmp_path, NOW, True, lambda url, **_: body.encode())
    assert catalog["ZAA"]["name"] == "Name 0" and catalog["QQQX"]["sector"] == "ETF"
    assert "TEST" not in catalog and fetched == T

    def down(url, **_):
        raise ProviderError("down")

    later = NOW + timedelta(days=2)
    catalog2, fetched2 = load_catalog(tmp_path, later, True, down)
    assert "ZAA" in catalog2 and fetched2 == T and "NVDA" in catalog2


# ------------------------------------------------------------------------------ store/service
def test_store_ingest_dedupes_and_deletes():
    store = RadarStore(":memory:")
    store.ingest("p", [item(1, "NVDA"), item(1, "NVDA again")], T, lambda _: ["NVDA"])
    assert len(store.items("p", 0)) == 1 and store.first_seen("p") == {"NVDA": T}
    store.ingest("p", [dict(item(1, ""), deleted=True)], T + 1, lambda _: [])
    assert store.items("p", 0) == []
    store.ingest("p", [item(2, "future", age=-3600)], T, lambda _: [])
    assert store.items("p", 0) == []


def test_daily_snapshot_prefers_last_one_before_cutoff():
    store = RadarStore(":memory:")
    row = [{"ticker": "NVDA", "mentions_24h": 1, "sentiment": 0.0, "rank": 1}]
    cutoff = 1000.0
    assert store.record_daily("p", "2026-10-19", 900, cutoff, row)
    assert store.record_daily("p", "2026-10-19", 990, cutoff, [dict(row[0], mentions_24h=2)])
    assert not store.record_daily("p", "2026-10-19", 1100, cutoff, [dict(row[0], mentions_24h=9)])
    assert store.daily_mentions("p", ["2026-10-19"]) == {"NVDA": {"2026-10-19": 2}}


def _radar(tmp_path, request, **kw) -> Radar:
    settings = RadarSettings(enabled=True, catalog_refresh=False, **kw)
    return Radar(settings, tmp_path, request=request, store=RadarStore(":memory:"))


def test_scan_success_failure_and_snapshot(tmp_path):
    posts = [
        {
            "id": "p1",
            "created_utc": T - 60,
            "subreddit": "wallstreetbets",
            "title": "$NVDA calls",
            "author": "u1",
        }
    ]
    radar = _radar(tmp_path, _arctic_requester(posts, [], []))
    assert radar.due(NOW) and radar.scan(NOW)
    assert not radar.due(NOW + timedelta(minutes=10)) and radar.due(NOW + timedelta(hours=1))
    snap = radar.snapshot(NOW)
    assert snap["status"]["state"] == "ok" and snap["metrics"]["items_24h"] == 1
    assert snap["tickers"][0]["ticker"] == "NVDA" and snap["coverage"]["warmup"]
    assert snap["tickers"][0]["attention_z"] is None  # no baseline days yet

    def failing(url, **_):
        raise ProviderError("Provider rate limit reached; retry on the next scheduled scan.")

    radar.request = failing
    later = NOW + timedelta(hours=1)
    assert not radar.scan(later)
    status = radar.snapshot(later)["status"]
    assert status["state"] == "error" and "rate limit" in status["error"]
    assert status["last_success"] == T  # the last good scan is kept


def test_unexpected_errors_are_not_serialised(tmp_path):
    def leaky(url, **_):
        raise RuntimeError("https://x?token=SECRET")

    radar = _radar(tmp_path, leaky)
    assert not radar.scan(NOW)
    assert "SECRET" not in json.dumps(radar.snapshot(NOW))


def test_daily_features_build_attention_baseline(tmp_path):
    radar = _radar(tmp_path, lambda url, **_: {"data": []})
    for days_ago in range(1, 11):
        radar.store.record_daily(
            "arctic_shift",
            (NOW - timedelta(days=days_ago)).date().isoformat(),
            T,
            T,
            [{"ticker": "NVDA", "mentions_24h": 2, "sentiment": 0.0, "rank": 1}],
        )
    radar.store.ingest("arctic_shift", [item(i, "$NVDA") for i in range(12)], T, lambda _: [])
    row = radar.snapshot(NOW)["tickers"][0]
    assert row["history_days"] == 10 and row["attention_z"] == 10.0


def test_lease_blocks_concurrent_scans(tmp_path):
    radar = _radar(tmp_path, lambda url, **_: {"data": []})
    assert radar._acquire_lease("other", T)
    assert not radar.scan(NOW)
