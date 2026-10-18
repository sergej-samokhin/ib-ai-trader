"""Radar configuration: ``RADAR_*`` and ``REDDIT_*`` keys from .env and the environment.

Kept apart from ``ibtrader.config.Settings`` on purpose: producer-side credentials and knobs
never become part of the trading configuration, and nothing here can touch a risk limit.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ibtrader.config import read_dotenv


class RadarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: Literal["arctic_shift", "reddit", "rss", "demo"] = "arctic_shift"
    subreddit: str = Field("wallstreetbets", pattern=r"^[A-Za-z0-9_]{2,30}$")
    interval_seconds: int = Field(3600, ge=600)  # below 10 minutes invites rate limits
    retention_days: int = Field(7, ge=3, le=60)  # raw items; daily features are kept 400 days
    user_agent: str = "ibtrader-radar/0.1 (personal research)"
    catalog_refresh: bool = True  # daily Nasdaq symbol directory download

    arctic_max_pages: int = Field(5, ge=1, le=5)
    arctic_page_size: int = Field(100, ge=1, le=100)
    arctic_overlap_seconds: int = Field(1200, ge=60, le=86400)
    arctic_timeout_seconds: int = Field(75, ge=10, le=90)

    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_refresh_token: str = ""
    reddit_max_pages: int = Field(2, ge=1, le=5)

    @field_validator("provider", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


_PREFIXED = {f"RADAR_{name.upper()}": name for name in RadarSettings.model_fields} | {
    f"{name.upper()}": name for name in RadarSettings.model_fields if name.startswith("reddit_")
}


def load_radar_settings(
    environ: Mapping[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> RadarSettings:
    """``RADAR_ENABLED``, ``RADAR_PROVIDER``, ... and ``REDDIT_CLIENT_ID``, ... (env wins)."""
    merged: dict[str, str] = {}
    if dotenv is not None:
        merged.update(read_dotenv(dotenv))
    merged.update(os.environ if environ is None else environ)
    kwargs = {_PREFIXED[key]: value for key, value in merged.items() if key in _PREFIXED}
    return RadarSettings(**kwargs)
