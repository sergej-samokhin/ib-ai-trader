"""Bounded Reddit adapters. Credentials stay in headers; errors never echo responses.

Every adapter returns ``{"provider", "items", "coverage"}``. Items are normalised dicts with
``id`` (t1_/t3_), ``kind`` (post|comment), ``title``, ``text``, ``url`` (reddit.com only),
``created_utc`` (epoch seconds), ``score`` (None when unavailable) and, where the source has it,
``author_hash``: a truncated SHA-256 of the lower-cased user name. User names are never stored.

Arctic Shift API: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md
Reddit listings:  https://www.reddit.com/dev/api/#listings
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 - parses Reddit's Atom feed, no DTDs/entities
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from ibtrader.radar.config import RadarSettings

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api/"
_REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com")
_ATOM = "{http://www.w3.org/2005/Atom}"

Requester = Callable[..., Any]


class ProviderError(RuntimeError):
    """A safe, user-visible provider failure without credential-bearing details."""


def http_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30,
    raw: bool = False,
) -> Any:
    """GET/POST one https URL with a size cap. Maps every failure to a sanitised ProviderError."""
    if not url.startswith("https://"):
        raise ProviderError("Only https provider URLs are allowed.")
    req = Request(url, data=data, headers=headers or {})  # noqa: S310 - https enforced above
    try:
        with urlopen(req, timeout=timeout) as response:  # noqa: S310
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 429:
            message = "Provider rate limit reached; retry on the next scheduled scan."
        elif exc.code in (401, 403):
            message = "Provider denied access; check credentials and API authorization."
        else:
            message = f"Provider returned HTTP {exc.code}; scan was not completed."
        raise ProviderError(message) from None
    except (URLError, OSError, UnicodeError):
        raise ProviderError("Provider connection failed; scan was not completed.") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProviderError("Provider response exceeded the size limit.")
    if raw:
        return payload
    try:
        return json.loads(payload)
    except ValueError:
        raise ProviderError("Provider returned invalid JSON; scan was not completed.") from None


def _timestamp(value: Any) -> float | None:
    try:
        if isinstance(value, str) and not re.fullmatch(r"[0-9.]+", value):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            number = moment.timestamp()
        else:
            number = float(value)
        if number > 100_000_000_000:  # milliseconds
            number /= 1000
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def author_hash(author: Any) -> str | None:
    if not isinstance(author, str) or author in ("", "[deleted]", "AutoModerator"):
        return None
    return hashlib.sha256(author.casefold().encode()).hexdigest()[:24]


def _community_matches(row: dict, subreddit: str) -> bool:
    community = row.get("subreddit")
    return isinstance(community, str) and (
        community.removeprefix("r/").casefold() == subreddit.casefold()
    )


def normalize(row: dict, kind: str | None, subreddit: str) -> dict | None:
    """Map one raw Reddit-like row to the item shape, or None if it is unusable."""
    if kind not in ("post", "comment"):
        return None
    raw_id = str(row.get("name") or row.get("id") or "")
    if not re.fullmatch(r"(?:t[13]_)?[A-Za-z0-9]+", raw_id):
        return None
    prefix = "t3_" if kind == "post" else "t1_"
    item_id = raw_id if raw_id.startswith(("t1_", "t3_")) else prefix + raw_id
    created = _timestamp(row.get("created_utc", row.get("createdAt")))
    if created is None:
        return None
    community = row.get("subreddit")
    if community and str(community).removeprefix("r/").casefold() != subreddit.casefold():
        return None
    url = str(row.get("permalink") or row.get("url") or "")
    if url.startswith("/"):
        url = "https://www.reddit.com" + url
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _REDDIT_HOSTS:
        url = f"https://www.reddit.com/r/{subreddit}/"
    title = str(row.get("title") or "")[:1000]
    body = str(row.get("selftext") or row.get("body") or "")[:30000]
    deleted = body in ("[deleted]", "[removed]") or row.get("removed_by_category") is not None
    if deleted:
        body, title = "", ""
    item: dict[str, Any] = {
        "id": item_id,
        "kind": kind,
        "title": title,
        "text": body,
        "url": url,
        "created_utc": created,
        "score": _int_or_none(row.get("score")),
        "source": "reddit",
    }
    if deleted:
        item["deleted"] = True
    hashed = author_hash(row.get("author"))
    if hashed:
        item["author_hash"] = hashed
    return item


def _arctic_shift(
    settings: RadarSettings, now: float, request: Requester, since: float | None
) -> dict:
    """A capped recent archive sample. Engagement counters are not used (provisional values).

    ``since`` (the last successful scan) makes the scan incremental, with
    ``arctic_overlap_seconds`` of overlap. There is no ID cursor: rows sharing a second at a page
    boundary can be missed, and the coverage block says so.
    """
    subreddit = settings.subreddit
    pages, limit = settings.arctic_max_pages, settings.arctic_page_size
    current = int(now)
    after = current - 86400
    if since is not None:
        if since > now:
            raise ProviderError("The last successful scan lies in the future; check the clock.")
        after = max(after, int(since) - settings.arctic_overlap_seconds)
    deadline = time.monotonic() + 240
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    feeds: dict[str, dict[str, Any]] = {
        name: {"pages": 0, "fetched": 0, "accepted": 0, "before": current + 1, "complete": False}
        for name in ("posts", "comments")
    }
    items: dict[str, dict] = {}
    skipped, warnings, delays = 0, [], []
    # Interleave feeds so a slow post archive cannot use up all request slots.
    for _ in range(pages):
        for feed, stats in feeds.items():
            if stats["complete"] or stats.get("stalled"):
                continue
            remaining = deadline - time.monotonic()
            if remaining < 1:
                stats["deadline_reached"] = True
                continue
            kind = "post" if feed == "posts" else "comment"
            fields = "id,created_utc,subreddit,retrieved_on,author," + (
                "title,selftext,url" if kind == "post" else "body,link_id,parent_id"
            )
            params = urlencode(
                {
                    "subreddit": subreddit,
                    "sort": "desc",
                    "limit": limit,
                    "after": after,
                    "before": stats["before"],
                    "fields": fields,
                }
            )
            # A denied, rate-limited or timed-out request ends the scan: no retry, no fallback.
            response = request(
                f"{ARCTIC_BASE}{feed}/search?{params}",
                headers=headers,
                timeout=min(settings.arctic_timeout_seconds, remaining),
            )
            rows = response.get("data") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise ProviderError("Arctic Shift returned an unexpected search response.")
            stats["pages"] += 1
            stats["fetched"] += len(rows)
            timestamps = []
            expected = "t3_" if kind == "post" else "t1_"
            for row in rows[:limit]:
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                created = _timestamp(row.get("created_utc"))
                if (
                    created is None
                    or not after <= created <= current
                    or not _community_matches(row, subreddit)
                ):
                    skipped += 1
                    continue
                timestamps.append(created)
                raw_id = str(row.get("id", ""))
                bare_id = raw_id.removeprefix(expected)
                if raw_id.startswith(("t1_", "t3_")) and not raw_id.startswith(expected):
                    skipped += 1
                    continue
                if not re.fullmatch(r"[A-Za-z0-9]+", bare_id):
                    skipped += 1
                    continue
                # Whitelist fields before normalisation, even if the server ignores 'fields'.
                mapped = {
                    key: row.get(key)
                    for key in (
                        "id",
                        "created_utc",
                        "subreddit",
                        "title",
                        "selftext",
                        "body",
                        "removed_by_category",
                        "author",
                    )
                }
                if kind == "post":
                    mapped["permalink"] = f"/r/{subreddit}/comments/{bare_id}/"
                else:
                    link_id = str(row.get("link_id", "")).removeprefix("t3_")
                    if not re.fullmatch(r"[A-Za-z0-9]+", link_id):
                        skipped += 1
                        continue
                    mapped["title"] = ""
                    mapped["permalink"] = f"/r/{subreddit}/comments/{link_id}/_/{bare_id}/"
                item = normalize(mapped, kind, subreddit)
                if not item:
                    skipped += 1
                    continue
                item.update(source="arctic-shift", score=None)
                retrieved = _timestamp(row.get("retrieved_on"))
                if retrieved is not None and created <= retrieved <= current:
                    delays.append(retrieved - created)
                if item["id"] not in items:
                    stats["accepted"] += 1
                items[item["id"]] = item
            if len(rows) < limit:
                stats["complete"] = True
            elif not timestamps:
                stats["stalled"] = True
            else:
                # Include the oldest second again so boundary rows are deduplicated, not lost.
                next_before = int(min(timestamps)) + 1
                stats["timestamp_boundary_gaps_possible"] = True
                if next_before >= stats["before"]:
                    stats["stalled"] = True
                else:
                    stats["before"] = next_before
    for feed, stats in feeds.items():
        stats["cap_reached"] = not stats["complete"] and stats["pages"] >= pages
        if not stats["complete"]:
            warnings.append(f"{feed}: bounded scan may omit older rows or rows sharing a second.")
    newest = max((item["created_utc"] for item in items.values()), default=None)
    return {
        "provider": "arctic_shift",
        "items": list(items.values()),
        "coverage": {
            "sampled": True,
            "archive": True,
            "subreddit": subreddit,
            "window_start_utc": after,
            "window_end_utc": current,
            "incremental": since is not None,
            "feeds": feeds,
            "skipped": skipped,
            "partial": bool(warnings),
            "warnings": warnings,
            "engagement_available": False,
            "newest_item_age_seconds": current - newest if newest is not None else None,
            "max_observed_archive_delay_seconds": max(delays, default=None),
            "note": "Arctic Shift: free, experimental archive feed. Rankings use mentions and text "
            "sentiment of the collected sample. Delayed or missing discussions are possible. "
            "Vote counts are unavailable.",
        },
    }


class _FeedText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _rss(settings: RadarSettings, now: float, request: Requester) -> dict:
    subreddit, limit = settings.subreddit, 100
    items: dict[str, dict] = {}
    feeds: dict[str, dict] = {}
    skipped = 0
    ns = {"a": _ATOM[1:-1]}
    headers = {"User-Agent": settings.user_agent, "Accept": "application/atom+xml, application/xml"}
    for name in ("new", "hot", "comments"):
        try:
            payload = request(
                f"https://www.reddit.com/r/{subreddit}/{name}/.rss?limit={limit}",
                headers=headers,
                raw=True,
            )
            if b"<!DOCTYPE" in payload[:2048] or b"<!ENTITY" in payload:
                raise ProviderError("Reddit RSS contained a DTD; refused.")
            root = ET.fromstring(payload)  # noqa: S314 - DTDs rejected above
            if root.tag != _ATOM + "feed":
                raise ProviderError("Reddit RSS returned an unexpected feed format.")
        except (ProviderError, ET.ParseError) as exc:
            if name != "comments":
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError("Reddit RSS response could not be parsed.") from None
            # The comment feed is optional; its absence stays visible in the coverage block.
            feeds[name] = {"available": False, "fetched": 0}
            continue
        entries = root.findall("a:entry", ns)
        accepted = 0
        for entry in entries[:limit]:
            matched = re.search(r"\b(t[13]_[A-Za-z0-9]+)\b", entry.findtext("a:id", "", ns))
            if not matched:
                skipped += 1
                continue
            kind = "comment" if matched.group(1).startswith("t1_") else "post"
            parser = _FeedText()
            parser.feed(entry.findtext("a:content", "", ns))
            body = " ".join("".join(parser.parts).split())
            # The standard RSS footer holds submitter metadata, not discussion text.
            body = re.split(r"\s+submitted by\s+", body, maxsplit=1, flags=re.I)[0]
            body = re.sub(r"\[link\]\s*\[comments\]\s*$", "", body).strip()
            links = entry.findall("a:link", ns)
            url = next(
                (lk.get("href", "") for lk in links if lk.get("rel", "alternate") == "alternate"),
                "",
            )
            row = {
                "id": matched.group(1),
                "title": entry.findtext("a:title", "", ns) if kind == "post" else "",
                "body": body,
                "url": url,
                "createdAt": entry.findtext("a:published", "", ns)
                or entry.findtext("a:updated", "", ns),
            }
            item = normalize(row, kind, subreddit)
            if item and item["created_utc"] <= now + 60:
                item["source"] = "reddit-rss"
                items[item["id"]] = item
                accepted += 1
            else:
                skipped += 1
        feeds[name] = {
            "available": True,
            "fetched": len(entries),
            "accepted": accepted,
            "cap_reached": len(entries) >= limit,
        }
    return {
        "provider": "rss",
        "items": list(items.values()),
        "coverage": {
            "sampled": True,
            "subreddit": subreddit,
            "feeds": feeds,
            "skipped": skipped,
            "engagement_available": False,
            "partial": True,
            "warnings": ["Public RSS returns at most 100 entries per feed."],
            "note": "Public RSS sample only. Scores, comment counts and authors are unavailable; "
            "truncated bodies and missed items are possible.",
        },
    }


def _reddit(settings: RadarSettings, now: float, request: Requester) -> dict:
    missing = [
        name
        for name, value in (
            ("REDDIT_CLIENT_ID", settings.reddit_client_id),
            ("REDDIT_CLIENT_SECRET", settings.reddit_client_secret),
            ("REDDIT_REFRESH_TOKEN", settings.reddit_refresh_token),
        )
        if not value
    ]
    if missing:
        raise ProviderError("Reddit configuration missing: " + ", ".join(missing))
    subreddit = settings.subreddit
    basic = base64.b64encode(
        f"{settings.reddit_client_id}:{settings.reddit_client_secret}".encode()
    ).decode()
    auth = request(
        "https://www.reddit.com/api/v1/access_token",
        headers={
            "User-Agent": settings.user_agent,
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=urlencode(
            {"grant_type": "refresh_token", "refresh_token": settings.reddit_refresh_token}
        ).encode(),
    )
    if not isinstance(auth, dict) or not isinstance(auth.get("access_token"), str):
        raise ProviderError("Reddit OAuth token exchange failed; check refresh token and scope.")
    headers = {"User-Agent": settings.user_agent, "Authorization": "Bearer " + auth["access_token"]}
    items: dict[str, dict] = {}
    listings: dict[str, dict] = {}
    skipped = 0
    for listing in ("new", "hot", "comments"):
        after, seen_cursors, fetched, page_count = None, set(), 0, 0
        for _ in range(settings.reddit_max_pages):
            params: dict[str, Any] = {"limit": 100, "raw_json": 1}
            if after:
                params["after"] = after
            response = request(
                f"https://oauth.reddit.com/r/{subreddit}/{listing}?{urlencode(params)}",
                headers=headers,
            )
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("children"), list):
                raise ProviderError("Reddit returned an unexpected listing format.")
            page_count += 1
            for child in data["children"]:
                if not isinstance(child, dict) or not isinstance(child.get("data"), dict):
                    skipped += 1
                    continue
                fetched += 1
                kind = {"t1": "comment", "t3": "post"}.get(child.get("kind"))
                item = normalize(child["data"], kind, subreddit)
                if item and item["created_utc"] <= now + 60:
                    items[item["id"]] = item
                else:
                    skipped += 1
            after = data.get("after")
            if not after or after in seen_cursors:
                break
            seen_cursors.add(after)
        listings[listing] = {"pages": page_count, "fetched": fetched, "has_more": bool(after)}
    return {
        "provider": "reddit",
        "items": list(items.values()),
        "coverage": {
            "sampled": True,
            "subreddit": subreddit,
            "listings": listings,
            "skipped": skipped,
            "partial": any(v["has_more"] for v in listings.values()),
            "note": "Bounded new/hot posts and recent comments via the Reddit API; not the whole "
            "subreddit.",
        },
    }


DEMO_SAMPLES = [
    ("NVDA", "bullish earnings beat, buying calls", 95),
    ("TSLA", "bearish demand miss, puts look good", 62),
    ("PLTR", "strong growth, bullish breakout", 44),
    ("AMD", "uncertain outlook, watching earnings", 30),
    ("GME", "squeeze moon rocket bullish", 21),
    ("RDDT", "overvalued, bearish downside", 16),
]


def demo(now: float) -> dict:
    """Deterministic synthetic records per 10-minute slot: rescans never multiply mentions."""
    slot = int(now) // 600
    items = []
    for offset in range(18):
        stamp = (slot - offset) * 600
        for index, (ticker, phrase, score) in enumerate(DEMO_SAMPLES):
            is_post = offset % 3 == 0
            items.append(
                {
                    "id": f"demo_{slot - offset}_{index}",
                    "kind": "post" if is_post else "comment",
                    "title": f"SYNTHETIC DEMO: ${ticker}" if is_post else "",
                    "text": f"SYNTHETIC DEMO: ${ticker} {phrase}.",
                    "url": "",
                    "created_utc": stamp,
                    "score": score,
                    "source": "synthetic-demo",
                    "author_hash": f"demo-author-{index}-{offset % 4}",
                }
            )
    return {
        "provider": "demo",
        "items": items,
        "coverage": {
            "sampled": True,
            "synthetic": True,
            "note": "Synthetic demonstration records. No live Reddit data and no market signal.",
        },
    }


def fetch(
    settings: RadarSettings,
    now: float,
    request: Requester = http_request,
    since: float | None = None,
) -> dict:
    """Fetch one bounded sample. A failure raises; demo data is never substituted.

    ``since`` is the time of the last successful scan (Arctic Shift scans incrementally).
    """
    if settings.provider == "arctic_shift":
        return _arctic_shift(settings, now, request, since)
    if settings.provider == "reddit":
        return _reddit(settings, now, request)
    if settings.provider == "rss":
        return _rss(settings, now, request)
    if settings.provider == "demo":
        return demo(now)
    raise ProviderError(f"Unknown radar provider {settings.provider!r}.")
