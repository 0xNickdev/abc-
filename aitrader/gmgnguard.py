"""Глобальный предохранитель GMGN: ban-гейт + лимитер частоты для ВСЕХ вызовов gmgn-cli.

Зачем (инцидент 15–28.09): GMGN забанил IP, а кулдаун был только у трендинга. Проверки
security/price по позициям (каждый поллинг /api/positions из каждой вкладки UI) продолжали
бить GMGN, а он продлевает бан на каждый запрос во время бана («repeated requests can extend
the ban by 5s up to 5 minutes») → бан не снимался 13 дней, бот молча стоял.

Теперь:
  - любой ответ 429/BANNED ставит ОБЩУЮ паузу до времени сброса из текста ошибки
    (`~300s remaining` / `resets at ...`) + запас; все вызовы в паузе падают сразу,
    не долетая до GMGN (ошибка содержит «429», поэтому существующие фолбэки срабатывают);
  - лимитер: вызовы не чаще MIN_INTERVAL_S (очередь слотов), чтобы не нарываться на бан;
  - snapshot() — здоровье для /api/status и алертов (последний успех, бан с какого момента).
"""
from __future__ import annotations

import datetime
import os
import re
import threading
import time

MIN_INTERVAL_S = float(os.getenv("ABC_GMGN_MIN_INTERVAL_S", "0.35") or 0.35)   # ~3 req/s
MAX_QUEUE_WAIT_S = float(os.getenv("ABC_GMGN_MAX_WAIT_S", "15") or 15)
BAN_PAD_S = 10.0          # запас сверх объявленного времени сброса
BAN_DEFAULT_S = 120.0     # сброс не распарсился
BAN_MAX_S = 900.0

_REMAINING = re.compile(r"~\s*(\d+)\s*s\s+remaining", re.I)
_RESETS_AT = re.compile(r"resets at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.I)


class GMGNUnavailable(RuntimeError):
    """Локальный отказ без запроса в сеть (пауза бана / переполнена очередь лимитера).
    Текст содержит «429», чтобы app._is_rate_limited и фолбэки обрабатывали как бан."""


def is_ban_text(s: str) -> bool:
    s = (s or "").lower()
    return "429" in s or "rate_limit" in s or "rate limit" in s or "banned" in s


def ban_seconds(text: str, now_wall: float | None = None) -> float:
    """Сколько ждать по тексту ошибки GMGN (с запасом, в пределах [30, BAN_MAX_S])."""
    now_wall = time.time() if now_wall is None else now_wall
    secs = None
    m = _REMAINING.search(text or "")
    if m:
        secs = float(m.group(1))
    else:
        m = _RESETS_AT.search(text or "")
        if m:
            try:
                at = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
                secs = at - now_wall
            except ValueError:
                secs = None
    if secs is None or secs <= 0:
        secs = BAN_DEFAULT_S
    return max(30.0, min(BAN_MAX_S, secs + BAN_PAD_S))


class Guard:
    def __init__(self, min_interval_s: float = MIN_INTERVAL_S,
                 max_wait_s: float = MAX_QUEUE_WAIT_S):
        self.min_interval_s = min_interval_s
        self.max_wait_s = max_wait_s
        self._lock = threading.Lock()
        self._banned_until = 0.0         # monotonic
        self._next_slot = 0.0            # monotonic
        self.calls = self.ok_count = self.fails = self.bans = self.skipped = 0
        self.last_ok_wall: float | None = None
        self.down_since_wall: float | None = None   # первый бан/сбой после последнего успеха
        self.last_error = ""

    # ── до вызова ──────────────────────────────────────────────────────────
    def before(self):
        now = time.monotonic()
        with self._lock:
            if now < self._banned_until:
                self.skipped += 1
                left = self._banned_until - now
                raise GMGNUnavailable(
                    f"429 GMGN paused locally for {left:.0f}s after RATE_LIMIT_BANNED "
                    f"(not sending requests so the ban can expire)")
            slot = max(now, self._next_slot)
            wait = slot - now
            if wait > self.max_wait_s:
                self.skipped += 1
                raise GMGNUnavailable(f"429 GMGN local throttle: queue {wait:.0f}s")
            self._next_slot = slot + self.min_interval_s
            self.calls += 1
        if wait > 0:
            time.sleep(wait)

    # ── после вызова ───────────────────────────────────────────────────────
    def ok(self):
        with self._lock:
            self.ok_count += 1
            self.last_ok_wall = time.time()
            self.down_since_wall = None
            self.last_error = ""

    def fail(self, err_text: str):
        with self._lock:
            self.fails += 1
            self.last_error = (err_text or "")[:300]
            if is_ban_text(err_text):
                self.bans += 1
                until = time.monotonic() + ban_seconds(err_text)
                self._banned_until = max(self._banned_until, until)
                if self.down_since_wall is None:
                    self.down_since_wall = time.time()

    def banned(self) -> bool:
        return time.monotonic() < self._banned_until

    def snapshot(self) -> dict:
        now_w = time.time()
        left = max(0.0, self._banned_until - time.monotonic())
        return dict(
            banned=left > 0, banned_left_s=round(left),
            last_ok_ago_s=(round(now_w - self.last_ok_wall) if self.last_ok_wall else None),
            down_for_s=(round(now_w - self.down_since_wall) if self.down_since_wall else None),
            calls=self.calls, ok=self.ok_count, fails=self.fails, bans=self.bans,
            skipped=self.skipped, last_error=self.last_error[:160])


GUARD = Guard()
