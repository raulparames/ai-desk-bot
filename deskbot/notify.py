"""Optional Telegram messages. Silent no-op unless both env vars are set."""

from __future__ import annotations

import logging

import requests

from .config import env

log = logging.getLogger(__name__)


def send(text: str) -> None:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": chunk}, timeout=15)
        except requests.RequestException as exc:
            log.warning("Telegram send failed: %s", exc)
