"""Notifier: fills, rejects, errors, approvals, kill-switch events, daily summary."""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Protocol

from ibtrader.config import Settings

log = logging.getLogger("ibtrader.notify")


class Notifier(Protocol):
    def send(self, subject: str, text: str, level: str = "info") -> None: ...


class LogNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, subject: str, text: str, level: str = "info") -> None:
        self.sent.append((subject, text, level))
        log.info("notify", extra={"fields": {"subject": subject, "text": text, "level": level}})


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(self, subject: str, text: str, level: str = "info") -> None:
        body = json.dumps(
            {"chat_id": self._chat_id, "text": f"[{level.upper()}] {subject}\n{text}"[:4000]}
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed https URL
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            pass


class EmailNotifier:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, subject: str, text: str, level: str = "info") -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[ibtrader {level}] {subject}"
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = self.s.notify_email_to
        msg.set_content(text)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)


class MultiNotifier:
    """Fan out; a failing channel is logged and never breaks trading logic."""

    def __init__(self, channels: list[Notifier]):
        self.channels = channels

    def send(self, subject: str, text: str, level: str = "info") -> None:
        for channel in self.channels:
            try:
                channel.send(subject, text, level)
            except Exception:  # noqa: BLE001
                log.exception("notifier channel failed", extra={"fields": {"subject": subject}})


def build_notifier(settings: Settings) -> MultiNotifier:
    channels: list[Notifier] = [LogNotifier()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    if settings.smtp_host and settings.notify_email_to:
        channels.append(EmailNotifier(settings))
    return MultiNotifier(channels)
