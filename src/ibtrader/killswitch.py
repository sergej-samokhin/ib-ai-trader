"""Kill switch: a file flag. Present -> cancel working entry orders and block all new entries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ibtrader.store import iso


class KillSwitch:
    def __init__(self, flag_file: Path):
        self.flag_file = flag_file

    def is_active(self) -> bool:
        return self.flag_file.exists()

    def activate(self, reason: str, now: datetime) -> None:
        self.flag_file.parent.mkdir(parents=True, exist_ok=True)
        self.flag_file.write_text(f"{iso(now)} {reason}\n", encoding="utf-8")

    def deactivate(self) -> None:
        self.flag_file.unlink(missing_ok=True)

    def reason(self) -> str | None:
        if not self.is_active():
            return None
        return self.flag_file.read_text(encoding="utf-8").strip() or "(no reason given)"
