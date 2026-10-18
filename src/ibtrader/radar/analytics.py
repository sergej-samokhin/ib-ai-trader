"""Offline, deterministic mention analytics; sentiment is a lexical baseline.

Windows are UTC epoch intervals (now - hours * 3600, now], not calendar hours. One item id
contributes at most once per ticker. ``first_seen`` maps a symbol to the time this collector
first observed it, not an IPO date. ``growth`` is a percentage vs. the preceding hour, or None
without a baseline. ``sparkline`` holds 24 rolling-hour mention counts, oldest first. Company
descriptions are static reference data, never live company research.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

_SEED = [
    (
        "AAPL",
        "Apple",
        "Consumer technology",
        "Designs consumer devices and provides software and digital services.",
        "https://www.apple.com/",
    ),
    (
        "MSFT",
        "Microsoft",
        "Software and cloud",
        "Develops software and provides cloud computing services.",
        "https://www.microsoft.com/",
    ),
    (
        "NVDA",
        "NVIDIA",
        "Semiconductors",
        "Designs graphics processors and accelerated computing platforms.",
        "https://www.nvidia.com/",
    ),
    (
        "AMD",
        "Advanced Micro Devices",
        "Semiconductors",
        "Designs processors and graphics chips.",
        "https://www.amd.com/",
    ),
    (
        "INTC",
        "Intel",
        "Semiconductors",
        "Designs and manufactures semiconductor products.",
        "https://www.intel.com/",
    ),
    (
        "TSLA",
        "Tesla",
        "Automotive and energy",
        "Makes electric vehicles and energy storage products.",
        "https://www.tesla.com/",
    ),
    (
        "AMZN",
        "Amazon",
        "Commerce and cloud",
        "Operates online commerce and cloud computing businesses.",
        "https://www.amazon.com/",
    ),
    (
        "GOOGL",
        "Alphabet",
        "Internet services",
        "Parent company of Google, whose businesses include search and cloud services.",
        "https://abc.xyz/",
    ),
    (
        "GOOG",
        "Alphabet",
        "Internet services",
        "Parent company of Google, whose businesses include search and cloud services.",
        "https://abc.xyz/",
    ),
    (
        "META",
        "Meta Platforms",
        "Internet services",
        "Operates social platforms including Facebook and Instagram.",
        "https://about.meta.com/",
    ),
    (
        "PLTR",
        "Palantir Technologies",
        "Software",
        "Develops data integration and analytics software.",
        "https://www.palantir.com/",
    ),
    (
        "GME",
        "GameStop",
        "Retail",
        "Retailer associated with video games and gaming merchandise.",
        "https://www.gamestop.com/",
    ),
    (
        "AMC",
        "AMC Entertainment",
        "Entertainment",
        "Operates movie theaters.",
        "https://www.amctheatres.com/",
    ),
    (
        "SOFI",
        "SoFi Technologies",
        "Financial services",
        "Provides consumer financial services through a digital platform.",
        "https://www.sofi.com/",
    ),
    (
        "HOOD",
        "Robinhood Markets",
        "Financial services",
        "Provides a retail investing platform.",
        "https://robinhood.com/",
    ),
    (
        "COIN",
        "Coinbase Global",
        "Financial technology",
        "Provides cryptocurrency trading and related services.",
        "https://www.coinbase.com/",
    ),
    (
        "MSTR",
        "Strategy",
        "Software and digital assets",
        "Provides business intelligence software and holds bitcoin.",
        "https://www.strategy.com/",
    ),
    (
        "NFLX",
        "Netflix",
        "Entertainment",
        "Provides a streaming entertainment service.",
        "https://www.netflix.com/",
    ),
    (
        "AVGO",
        "Broadcom",
        "Semiconductors and software",
        "Supplies semiconductor and infrastructure software products.",
        "https://www.broadcom.com/",
    ),
    (
        "TSM",
        "Taiwan Semiconductor Manufacturing",
        "Semiconductors",
        "Manufactures semiconductor chips for customers.",
        "https://www.tsmc.com/",
    ),
    (
        "MU",
        "Micron Technology",
        "Semiconductors",
        "Makes memory and storage semiconductor products.",
        "https://www.micron.com/",
    ),
    (
        "ARM",
        "Arm Holdings",
        "Semiconductors",
        "Licenses processor architectures and semiconductor intellectual property.",
        "https://www.arm.com/",
    ),
    (
        "RDDT",
        "Reddit",
        "Internet services",
        "Operates an online community discussion platform.",
        "https://www.redditinc.com/",
    ),
    (
        "RKLB",
        "Rocket Lab",
        "Aerospace",
        "Provides launch services and space systems.",
        "https://www.rocketlabusa.com/",
    ),
    (
        "ASTS",
        "AST SpaceMobile",
        "Satellite communications",
        "Develops a satellite based cellular broadband network.",
        "https://ast-science.com/",
    ),
    (
        "SPY",
        "SPDR S&P 500 ETF Trust",
        "ETF",
        "Fund designed to track the S&P 500 Index; this is a fund, not a company.",
        "https://www.ssga.com/",
    ),
    (
        "QQQ",
        "Invesco QQQ",
        "ETF",
        "Fund designed to track the Nasdaq-100 Index; this is a fund, not a company.",
        "https://www.invesco.com/",
    ),
]

_SLANG = set(
    "CEO CFO CTO DD YOLO WSB FOMO FUD ATH IPO ETF EPS PE GDP CPI USA USD AI LOL LMAO IMO IMHO RIP "  # noqa: SIM905
    "SEC FED API HTTP HTTPS TLDR TTM RSI MACD ITM OTM DTE OP IV EDIT CALL PUT".split()
)
# Strategy section 3.2 blacklist plus common English words that are also listed symbols.
_AMBIGUOUS = set(
    "A I IT IS ON AT AS OR AN BE BY DO GO HE IF IN ME MY NO OF OK SO TO UP US WE ALL ARE CAN FOR "  # noqa: SIM905
    "HAS HOW ITS NEW NOW ONE OUT OWN SEE SHE THE TOO TWO WAS WAY WHO WHY YOU ARM OPEN LOVE NEXT "
    "GOOD BEST WELL REAL LIVE EVER NOTE TRUE HUGE JOBS ANY JUST WILL VERY NICE FIRST LAST MORE "
    "MOST SOME MUCH MANY EACH BOTH ALSO EVEN ONLY SAME SUCH THAN THEN THAT THEM THEY THIS THUS "
    "WHAT WHEN WITH FROM HAVE BEEN WERE INTO OVER YOUR HERE COME ELSE ONCE LIKE LOOK LONG SHORT "
    "BUY SELL HOLD CALLS PUTS PLAY PLAN RISK SAFE HOPE HYPE STEP UNIT TASK TEAM WORK LIFE TIME "
    "TALK CARE CASH GAIN BULL BEAR MOON TOP LOW HIGH BIG FAST SLOW FOUR FIVE TEN HALF FULL FREE "
    "KEEP KNOW LET MADE MAKE MEAN MOVE MUST NEAR NEED NEWS PART PAST POST READ RUN SAID SAY SEND "
    "SET SHOW SITE STAY STOP TAKE TELL TILL TURN VIEW WANT WISH YEAR BEAT BET BOOM EAT FUN KIND "
    "MAIN MIND PAY RARE REST RIDE RING ROCK SAVE SMART SOON SPOT SURE TECH TINY TRIP TRY TUNE "
    "TWIN USED WAIT WARM WIDE WILD WING WIRE WISE WOOD RH".split()
)
# Common WSB short symbols: still require membership in the catalog.
_SAFE_SHORT = {"MU", "BA", "GE", "GM"}
_TOKEN = re.compile(r"(?<![\w$])(?P<cash>\$)?(?P<symbol>[A-Za-z]{1,5}(?:[.-][A-Za-z])?)(?![\w.-])")
_URL = re.compile(r"https?://\S+", re.I)
_DELETED = {"[deleted]", "[removed]", "deleted", "removed"}
_SARCASM = re.compile(r"(?:/s\b|\bsarcas[mt]|yeah\s+right|as\s+if)", re.I)
_CLAUSES = re.compile(r"[.!?;\n,]|\b(?:but|whereas|while|however)\b", re.I)
_POSITIVE = {
    "bullish",
    "buy",
    "buying",
    "bought",
    "long",
    "calls",
    "moon",
    "mooning",
    "rally",
    "ripping",
    "undervalued",
    "outperform",
    "upside",
    "rocket",
    "squeeze",
}
_NEGATIVE = {
    "bearish",
    "sell",
    "selling",
    "sold",
    "short",
    "shorting",
    "puts",
    "crash",
    "crashing",
    "dump",
    "dumping",
    "overvalued",
    "downside",
    "bankrupt",
    "worthless",
    "avoid",
}
_NEGATIONS = {
    "not",
    "no",
    "never",
    "isn't",
    "isnt",
    "don't",
    "dont",
    "doesn't",
    "doesnt",
    "won't",
    "wont",
    "cannot",
    "can't",
    "cant",
}
_WORDS = re.compile(r"[a-z]+(?:'[a-z]+)?")
SENTIMENT_METHOD = "lexical-clause-baseline-v1"


def default_catalog() -> dict[str, dict[str, Any]]:
    """A fresh static starter catalog. ``as_of`` None means not live verified."""
    return {
        symbol: {
            "name": name,
            "sector": sector,
            "description": desc,
            "website": website,
            "source": website,
            "as_of": None,
        }
        for symbol, name, sector, desc, website in _SEED
    }


def merge_catalog(catalog: dict | None) -> dict[str, dict[str, Any]]:
    result = default_catalog()
    for symbol, value in (catalog or {}).items():
        key = str(symbol).upper()
        if isinstance(value, str):
            value = {"name": value}
        if not isinstance(value, dict):
            value = {}
        result[key] = {**result.get(key, {}), **value}
    return result


def is_etf(info: dict[str, Any] | None) -> bool:
    return bool(info) and str(info.get("sector", "")).upper() == "ETF"


def _mentions(text: str, catalog: dict) -> Iterator[tuple[str, int, int]]:
    for match in _TOKEN.finditer(_URL.sub(" ", text)):
        raw = match.group("symbol")
        symbol = raw.upper()
        cash = bool(match.group("cash"))
        if symbol not in catalog:
            continue
        if not cash and (
            raw != symbol
            or symbol in _SLANG
            or (len(symbol) <= 2 and symbol not in _SAFE_SHORT)
            or symbol in _AMBIGUOUS
        ):
            continue
        yield symbol, match.start(), match.end()


def extract_tickers(text: str, catalog: dict | None = None) -> list[str]:
    """Known upper-case symbols or validated $cashtags, in order of first appearance.

    Ambiguous words and short symbols need a dollar prefix (except a small curated set of common
    short symbols). A validated cashtag wins over slang filtering: ``$AI`` can name C3.ai while a
    bare ``AI`` is ignored.
    """
    if not isinstance(text, str) or text.strip().lower() in _DELETED:
        return []
    known = merge_catalog(catalog)
    return list(dict.fromkeys(symbol for symbol, _, _ in _mentions(text, known)))


def sentiment(text: str, symbol: str, catalog: dict) -> tuple[int, bool]:
    """Conservative clause-local score (+1, -1, 0) and an uncertainty flag.

    Sarcasm markers, clauses naming several tickers and mixed evidence are neutral.
    """
    if _SARCASM.search(text):
        return 0, True
    scores: list[int] = []
    uncertain = False
    for clause in _CLAUSES.split(text):
        symbols = {s for s, _, _ in _mentions(clause, catalog)}
        if symbol not in symbols:
            continue
        if len(symbols) > 1:
            uncertain = True
            continue
        words = _WORDS.findall(clause.lower().replace("’", "'"))
        clause_score = 0
        for index, word in enumerate(words):
            polarity = 1 if word in _POSITIVE else -1 if word in _NEGATIVE else 0
            if not polarity:
                continue
            if sum(w in _NEGATIONS for w in words[max(0, index - 3) : index]) % 2:
                polarity *= -1
            clause_score += polarity
        if clause_score:
            scores.append(1 if clause_score > 0 else -1)
    if uncertain or (1 in scores and -1 in scores):
        return 0, True
    return (scores[0] if scores else 0), False


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def item_text(item: dict) -> str:
    # A removed body must invalidate the post title too.
    body = str(item.get("text") or "")
    if body.strip().lower() in _DELETED:
        return ""
    title = str(item.get("title") or "")
    return "\n".join(dict.fromkeys(part for part in (title, body) if part))


def unique_items(items: list[dict]) -> list[dict]:
    """Last observation per id wins, so a later deletion removes the content."""
    unique: dict[str, dict] = {}
    for index, item in enumerate(items):
        if isinstance(item, dict):
            unique[str(item.get("id") or f"missing-id:{index}")] = item
    return list(unique.values())


def analyze(
    items: list[dict],
    now: float,
    first_seen: dict[str, float],
    catalog: dict | None = None,
    baseline_ready: bool = True,
) -> list[dict[str, Any]]:
    """Rank tickers observed in the rolling last 24 h. Pure; no I/O.

    ``first_seen`` is updated in place for new symbols; the caller persists it. ``sentiment`` is
    (bullish - bearish) / all mentions, neutral included. ``hot_score`` weights recency, unique
    mentions, bounded engagement and growth: it measures discussion activity, never merit.
    ``unique_authors`` is None when the source supplies no (hashed) author for the ticker.
    """
    now = _number(now)
    known = merge_catalog(catalog)
    buckets: dict[str, dict[str, Any]] = {}
    for item in unique_items(items):
        age = now - _number(item.get("created_utc"), -1)
        if not 0 <= age < 86400:
            continue
        content = item_text(item)
        for symbol in dict.fromkeys(s for s, _, _ in _mentions(content, known)):
            row = buckets.setdefault(
                symbol,
                {
                    "ticker": symbol,
                    "mentions_1h": 0,
                    "mentions_24h": 0,
                    "previous_hour": 0,
                    "bullish": 0,
                    "bearish": 0,
                    "neutral": 0,
                    "uncertain_count": 0,
                    "posts": 0,
                    "comments": 0,
                    "sparkline": [0] * 24,
                    "evidence": [],
                    "_activity": 0.0,
                    "_authors": set(),
                    "_attributed": 0,
                },
            )
            row["mentions_24h"] += 1
            row["mentions_1h"] += int(age < 3600)
            row["previous_hour"] += int(3600 <= age < 7200)
            row["sparkline"][23 - int(age // 3600)] += 1
            kind = "comment" if item.get("kind") == "comment" else "post"
            row["comments" if kind == "comment" else "posts"] += 1
            score, uncertain = sentiment(content, symbol, known)
            row[{1: "bullish", -1: "bearish", 0: "neutral"}[score]] += 1
            row["uncertain_count"] += int(uncertain)
            author = item.get("author_hash")
            if isinstance(author, str) and author:
                row["_authors"].add(author)
                row["_attributed"] += 1
            votes = item.get("score")
            engagement = min(3.0, math.log1p(max(0.0, _number(votes))) / 4)
            row["_activity"] += (1 + engagement) * math.exp(-age / 14400)
            row["evidence"].append(
                {
                    "text": content[:420],
                    "url": str(item.get("url") or ""),
                    "score": votes if isinstance(votes, int | float) else None,
                    "kind": kind,
                }
            )
    result = []
    for symbol, row in buckets.items():
        first_seen.setdefault(symbol, now)
        observed = _number(first_seen[symbol], now)
        row["first_seen"] = observed
        row["is_new"] = 0 <= now - observed < 86400
        previous = row["previous_hour"]
        growth = (
            round(100 * (row["mentions_1h"] - previous) / previous, 1)
            if previous and baseline_ready
            else None
        )
        row["growth"] = growth
        row["sentiment"] = round((row["bullish"] - row["bearish"]) / row["mentions_24h"], 3)
        row["sentiment_label"] = (
            "bullish"
            if row["sentiment"] > 0.15
            else "bearish"
            if row["sentiment"] < -0.15
            else "neutral"
        )
        row["sentiment_method"] = SENTIMENT_METHOD
        activity = row.pop("_activity")
        row["hot_score"] = round(activity * (1 + min(3, max(0, growth or 0) / 100) * 0.15), 2)
        # Breadth counts only mentions with a known author (deleted accounts have none).
        authors, attributed = row.pop("_authors"), row.pop("_attributed")
        row["unique_authors"] = len(authors) if authors else None
        row["breadth"] = round(len(authors) / attributed, 3) if authors else None
        row["evidence"] = sorted(row["evidence"], key=lambda e: _number(e["score"]), reverse=True)[
            :5
        ]
        row["company"] = {
            "name": symbol,
            "sector": "Unavailable",
            "description": "Company description unavailable.",
            "website": None,
            "source": None,
            "as_of": None,
            **deepcopy(known[symbol]),
        }
        row["is_etf"] = is_etf(known[symbol])
        result.append(row)
    by_mentions = sorted(result, key=lambda r: (-r["mentions_24h"], r["ticker"]))
    for rank, row in enumerate(by_mentions, start=1):
        row["rank"] = rank
    return sorted(result, key=lambda r: (-r["hot_score"], -r["mentions_24h"], r["ticker"]))


def count_mentions(items: list[dict], start: float, end: float, catalog: dict | None) -> dict:
    """Mentions per ticker of items created in [start, end); one per item and ticker."""
    known = merge_catalog(catalog)
    counts: dict[str, int] = {}
    for item in unique_items(items):
        created = _number(item.get("created_utc"), -1)
        if not start <= created < end:
            continue
        for symbol in dict.fromkeys(s for s, _, _ in _mentions(item_text(item), known)):
            counts[symbol] = counts.get(symbol, 0) + 1
    return counts
