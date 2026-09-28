"""Telegram-алерты оператору: бот не должен больше молча стоять неделями.

Env: TELEGRAM_BOT_TOKEN (от @BotFather) + TELEGRAM_CHAT_ID. Без них — no-op.
ABC_TG_TRADES=1 — дополнительно слать каждую сделку бота (BUY/SELL).
Отправка в фоне (не блокирует тик/запрос), ошибки глотаются. Дедуп по ключу с кулдауном.
"""
from __future__ import annotations

import os
import threading
import time

import httpx

_sent: dict[str, float] = {}
_lock = threading.Lock()


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def enabled() -> bool:
    return bool(_token() and _chat())


def trades_enabled() -> bool:
    return enabled() and os.getenv("ABC_TG_TRADES", "").strip().lower() in ("1", "true", "yes", "on")


def _post(text: str) -> bool:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{_token()}/sendMessage",
                       json=dict(chat_id=_chat(), text=text[:4000],
                                 disable_web_page_preview=True), timeout=10.0)
        return r.status_code == 200
    except Exception:
        return False


def send(text: str, key: str | None = None, cooldown_s: float = 0.0) -> bool:
    """Отправить в фоне. key+cooldown_s — не чаще раза в cooldown_s по одному ключу.
    Возвращает True, если сообщение поставлено в отправку."""
    if not enabled():
        return False
    if key:
        now = time.monotonic()
        with _lock:
            last = _sent.get(key)
            if last is not None and now - last < cooldown_s:
                return False
            _sent[key] = now
    threading.Thread(target=_post, args=("[abc] " + text,), daemon=True).start()
    return True


def reset(key: str):
    """Снять дедуп (состояние восстановилось — следующий сбой снова алертим)."""
    with _lock:
        _sent.pop(key, None)
