"""Ticker whitelist: the public Nasdaq symbol directory, cached daily, plus the static seed.

Strategy section 3.2 wants tickers of US stocks *with listed options*. The Nasdaq directory
covers all US-listed securities; options availability is checked later by the trader's
selector against the live chain.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ibtrader.radar.analytics import default_catalog
from ibtrader.radar.providers import ProviderError, http_request

SOURCES = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
_SYMBOL = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z])?")
NO_DESCRIPTION = (
    "Business description not available; security name verified against the Nasdaq directory."
)


def _parse(body: str, url: str, day: str) -> dict[str, dict[str, Any]]:
    rows = list(csv.DictReader(io.StringIO(body), delimiter="|"))
    if len(rows) < 100:
        raise ValueError("Symbol directory unexpectedly short")
    symbols = {}
    for row in rows:
        ticker = row.get("Symbol") or row.get("NASDAQ Symbol") or ""
        if row.get("Test Issue") == "Y" or not _SYMBOL.fullmatch(ticker):
            continue
        symbols[ticker] = {
            "name": row.get("Security Name", ticker),
            "sector": "ETF" if row.get("ETF") == "Y" else "Not specified",
            "description": NO_DESCRIPTION,
            "website": None,
            "source": url,
            "as_of": day,
        }
    return symbols


def load_catalog(
    directory: Path,
    now: datetime,
    refresh: bool = False,
    request: Callable[..., Any] = http_request,
) -> tuple[dict[str, dict[str, Any]], float | None]:
    """Return (catalog, fetched_at). A failed refresh keeps the last good cache."""
    path = directory / "catalog.json"
    cached: dict[str, Any] = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached = {}
    stamp = now.timestamp()
    if refresh and stamp - float(cached.get("fetched_at") or 0) > 86400:
        try:
            symbols: dict[str, dict[str, Any]] = {}
            day = now.astimezone(UTC).date().isoformat()
            for url in SOURCES:
                body = request(
                    url,
                    headers={"User-Agent": "ibtrader-radar symbol directory"},
                    timeout=20,
                    raw=True,
                )
                symbols.update(_parse(body.decode("utf-8-sig"), url, day))
            cached = {"fetched_at": stamp, "symbols": symbols}
            directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(cached), encoding="utf-8")
            temporary.replace(path)
        except (ProviderError, OSError, ValueError, UnicodeError):
            pass  # keep the last successful catalog and the curated seed
    result = dict(cached.get("symbols") or {})
    result.update(default_catalog())
    return result, cached.get("fetched_at")
