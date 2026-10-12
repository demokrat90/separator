"""Telegram alerts for anomalies only (autostop, cap, send errors, quality,
template status, LLM unavailable). Never raises; no token or chat = log only.

Alert texts carry deal ids, never customer phone numbers or names.
"""

import logging

import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger("django")

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
MAX_TEXT = 3500


def send_alert(text):
    """Send now. Returns True only if Telegram accepted the message."""
    try:
        from .models import WaqSettings

        text = f"[waq] {text}"[:MAX_TEXT]
        token = getattr(settings, "WAQ_TG_TOKEN", "") or ""
        chat_id = (WaqSettings.load().alert_chat_id or "").strip()
        if not token or not chat_id:
            logger.warning("waq alert (no telegram configured): %s", text)
            return False
        response = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            # The token is part of the URL; never log the request itself.
            logger.warning("waq alert: telegram answered %s", response.status_code)
            return False
        return True
    except Exception as exc:
        logger.warning("waq alert failed: %s: %s", type(exc).__name__, text)
        return False


def alert_on_commit(text):
    """Send after the surrounding transaction commits (row locks are not held
    during the HTTP call); outside a transaction it is sent immediately."""
    try:
        transaction.on_commit(lambda: send_alert(text))
    except Exception as exc:
        logger.warning("waq alert could not be scheduled: %s", exc)
