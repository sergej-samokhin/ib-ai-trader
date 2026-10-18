"""Runtime configuration: environment / .env plus the producer registry file.

Everything that can loosen risk lives here, never in a signal.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Gateway / TWS defaults, plus the paper/live relay ports of the compose gateway image.
PAPER_PORTS = {4002, 7497, 4004}
LIVE_PORTS = {4001, 7496, 4003}


class RiskLimits(BaseModel):
    """Global guardrails (spec section 6). Signals can only request less, never more."""

    model_config = ConfigDict(extra="forbid")

    max_position_usd: float = Field(500.0, gt=0)
    max_portfolio_risk_usd: float = Field(2000.0, gt=0)
    max_open_positions: int = Field(5, ge=1)
    max_daily_loss_usd: float = Field(300.0, gt=0)
    max_monthly_loss_usd: float = Field(3000.0, gt=0)
    max_underlying_exposure_usd: float = Field(1000.0, gt=0)
    min_open_interest: int = Field(100, ge=0)
    max_bid_ask_spread_pct: float = Field(10.0, gt=0)
    no_entry_open_minutes: int = Field(15, ge=0)
    no_entry_close_minutes: int = Field(15, ge=0)
    manual_approval_above_usd: float = Field(400.0, gt=0)
    min_confidence_auto: float = Field(0.0, ge=0, le=1)
    approval_timeout_minutes: int = Field(30, ge=1)
    max_delta_deviation: float = Field(0.10, gt=0, le=0.5)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["paper", "live"] = "paper"
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 17
    ib_account: str = ""
    market_data_type: int = Field(1, ge=1, le=4)  # 1 live, 2 frozen, 3 delayed, 4 delayed-frozen

    data_dir: Path = Path("var")
    producers_file: Path = Path("config/producers.yaml")

    http_enabled: bool = False
    http_host: str = "127.0.0.1"
    http_port: int = 8787
    file_drop_enabled: bool = True

    max_signal_bytes: int = Field(64 * 1024, ge=1024)
    max_clock_skew_seconds: int = Field(300, ge=0)
    loop_interval_seconds: float = Field(5.0, gt=0)
    reprice_interval_seconds: float = Field(20.0, gt=0)
    rule_eval_interval_seconds: float = Field(300.0, gt=0)
    price_tick: float = Field(0.05, gt=0)
    exit_max_reprice_steps: int = Field(5, ge=0, le=20)
    daily_summary_time: str = "16:15"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email_to: str = ""

    risk: RiskLimits = RiskLimits()

    # -- derived paths -------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "ibtrader.sqlite3"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def kill_switch_file(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "ibtrader.jsonl"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes. No interpolation."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


_RISK_FIELDS = set(RiskLimits.model_fields)
_SETTINGS_FIELDS = set(Settings.model_fields) - {"risk"}


def load_settings(
    environ: Mapping[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> Settings:
    """Build Settings from .env (lowest priority) and the process environment.

    Keys are the upper-case field names, e.g. ``MODE``, ``IB_PORT``, ``MAX_POSITION_USD``.
    ``MODE`` must be exactly ``live`` to trade live; anything else is a configuration error.
    """
    merged: dict[str, str] = {}
    if dotenv is not None:
        merged.update(read_dotenv(dotenv))
    merged.update(os.environ if environ is None else environ)

    settings_kwargs: dict[str, object] = {}
    risk_kwargs: dict[str, object] = {}
    for key, value in merged.items():
        name = key.lower()
        if name in _SETTINGS_FIELDS:
            settings_kwargs[name] = value
        elif name in _RISK_FIELDS:
            risk_kwargs[name] = value
    if "mode" in settings_kwargs:
        settings_kwargs["mode"] = str(settings_kwargs["mode"]).strip().lower()
    return Settings(**settings_kwargs, risk=RiskLimits(**risk_kwargs))


def check_mode_safety(settings: Settings) -> list[str]:
    """Static sanity checks between MODE and connection settings. Returns problems found."""
    problems: list[str] = []
    if settings.mode == "paper" and settings.ib_port in LIVE_PORTS:
        problems.append(
            f"MODE=paper but IB_PORT={settings.ib_port} is a standard live-trading port"
        )
    if settings.mode == "live" and settings.ib_port in PAPER_PORTS:
        problems.append(
            f"MODE=live but IB_PORT={settings.ib_port} is a standard paper-trading port"
        )
    if settings.mode == "live" and not settings.ib_account:
        problems.append("MODE=live requires IB_ACCOUNT to be set explicitly")
    if (
        settings.ib_account
        and settings.mode == "paper"
        and not settings.ib_account.startswith("DU")
    ):
        problems.append(
            f"MODE=paper but IB_ACCOUNT={settings.ib_account} does not look like a paper "
            "account (paper accounts start with 'DU')"
        )
    return problems
