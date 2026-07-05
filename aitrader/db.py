"""Снапшот-хранилище качества токенов (SQLite) — фундамент под verification-слой.

Зачем БД, а не jsonl: пользователь хочет «снапшотить все проверки и в моменте брать
из базы» + кластеризацию ферм (запросы «кошельки с одинаковой историей»), а это
реляционные выборки, которые по файлу не сделать. Каждый скрин-проход пишет компактный
ряд на решение (гейты/скор/безопасность/сигналы/атрибуция) — потом по этой таблице
считаем эдж, ловим паттерны, отдаём историю в UI.

Опт-ин: писать снапшоты включает ABC_SNAPSHOT_DB=1 (по умолчанию выкл → старое поведение,
тесты не трогаем). Путь — outputs/abc.db (или ABC_DB_PATH). Потокобезопасно: соединение
на операцию (наш объём мал), WAL — чтобы читатель не блокировал писателя.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import threading

_DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "outputs" / "abc.db"
_LOCK = threading.Lock()          # сериализуем запись (SQLite не любит конкурентных писателей)

# индексируемые колонки снапшота + сырой features-JSON (гибкость под будущие поля)
_COLS = ("ts", "chain", "address", "symbol", "action", "reason", "gate", "priority",
         "top10", "bundler", "dev_hold", "sniper", "smart_degen", "tracked_hits",
         "holder_count", "holder_velocity", "renounced", "x_mentions", "fresh_wallets")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(_COLS)},
    features TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_addr ON snapshots(address);
CREATE INDEX IF NOT EXISTS ix_snap_ts   ON snapshots(ts);
"""


def enabled() -> bool:
    return os.getenv("ABC_SNAPSHOT_DB", "").strip().lower() in ("1", "true", "yes", "on")


def _path(path=None) -> pathlib.Path:
    p = pathlib.Path(path or os.getenv("ABC_DB_PATH") or _DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(path=None) -> sqlite3.Connection:
    con = sqlite3.connect(_path(path), timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db(path=None) -> None:
    with _LOCK, _connect(path) as con:
        con.executescript(_SCHEMA)


def _row_from_decision(d: dict, chain: str, ts: str) -> dict:
    """Из decision-словаря screen_once (decision.features = _feat(f)) → плоский ряд снапшота."""
    dec = d.get("decision", d) or {}
    ft = dec.get("features", {}) or {}
    return dict(
        ts=ts, chain=chain, address=dec.get("address", ""), symbol=dec.get("symbol", ""),
        action=dec.get("action", ""), reason=dec.get("reason", ""),
        gate=dec.get("gate"), priority=dec.get("priority"),
        top10=ft.get("top10"), bundler=ft.get("bundler"), dev_hold=ft.get("dev_hold"),
        sniper=ft.get("sniper_count"), smart_degen=ft.get("smart_degen"),
        tracked_hits=ft.get("tracked_hits"), holder_count=ft.get("holder_count"),
        holder_velocity=ft.get("holder_velocity"),
        renounced=1 if ft.get("renounced") else 0,
        x_mentions=ft.get("x_mentions"), fresh_wallets=ft.get("fresh_wallets"),
        features=json.dumps(ft, ensure_ascii=False))


def record_decisions(decisions: list[dict], chain: str, ts: str, path=None) -> int:
    """Снапшот всех решений одного скрин-прохода одной транзакцией. Возвращает число рядов.
    Тихо возвращает 0 при любой ошибке БД — снапшот НИКОГДА не должен ронять скан."""
    if not decisions:
        return 0
    rows = [_row_from_decision(d, chain, ts) for d in decisions]
    cols = list(_COLS) + ["features"]
    sql = f"INSERT INTO snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    try:
        with _LOCK, _connect(path) as con:
            con.executescript(_SCHEMA)
            con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)
    except Exception:
        return 0


def recent(limit: int = 100, address: str | None = None, path=None) -> list[dict]:
    """Последние снапшоты (опц. по адресу) — новые первыми. Для UI-истории и анализа."""
    q = "SELECT * FROM snapshots"
    args: list = []
    if address:
        q += " WHERE address = ?"
        args.append(address)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _connect(path) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(q, args).fetchall()]
    except Exception:
        return []


def prune(days: int = 14, path=None) -> int:
    """Удалить снапшоты старше N дней (ts — ISO-строка, лексикографически сравнима). Возвращает удалённые."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)).isoformat()
    try:
        with _LOCK, _connect(path) as con:
            cur = con.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount
    except Exception:
        return 0
