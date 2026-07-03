"""KOL-сигнал из Twitter/X (per-user opt-in): кто упоминает контракт прямо сейчас.

Каждый юзер подключает СВОЙ Bearer-токен Twitter API v2 (вкладка KOL/X в настройках):
лимиты Twitter — на токен, поэтому ключ оператора не выгорает от чужих запросов.
Токен хранится на сервере per-user (chmod 600) и отдаётся наружу только как has_key.

Запрос — recent search по адресу контракта (CA — уникальная строка, шум почти нулевой).
Дорогая квота → жёсткий кэш TTL 5 мин на (токен, CA), и дергается ТОЛЬКО по кнопке
в деталях строки, никогда автоматически на каждый ряд.
"""
from __future__ import annotations

import time

import httpx

SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
TTL = 300.0
_cache: dict[tuple, tuple] = {}   # (token_fp, ca) -> (monotonic_ts, payload)


def _fp(bearer: str) -> int:
    return hash(bearer) & 0xFFFFFFFF


def mentions(ca: str, bearer: str) -> dict:
    """Свежие твиты с CA + топ-авторы по подписчикам. Кэш 5 мин; 429 → мягкая ошибка."""
    key = (_fp(bearer), ca)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return dict(hit[1], cached=True)
    r = httpx.get(SEARCH_URL, params={
        "query": f'"{ca}" -is:retweet', "max_results": 25,
        "tweet.fields": "public_metrics,created_at", "expansions": "author_id",
        "user.fields": "username,public_metrics,verified"},
        headers={"Authorization": f"Bearer {bearer}"}, timeout=12.0)
    if r.status_code == 429:
        return dict(ok=False, error="rate_limited",
                    detail="Лимит Twitter API исчерпан — подожди 15 минут")
    if r.status_code in (401, 403):
        return dict(ok=False, error="auth",
                    detail="Twitter отклонил токен (нужен тариф с recent search)")
    r.raise_for_status()
    j = r.json()
    users = {u["id"]: u for u in (j.get("includes", {}) or {}).get("users", [])}
    tweets = j.get("data", []) or []
    seen, authors = set(), []
    for tw in tweets:
        u = users.get(tw.get("author_id"))
        if not u or u["id"] in seen:
            continue
        seen.add(u["id"])
        pm = u.get("public_metrics", {}) or {}
        authors.append(dict(username=u.get("username", "?"),
                            followers=int(pm.get("followers_count", 0)),
                            verified=bool(u.get("verified", False))))
    authors.sort(key=lambda a: -a["followers"])
    payload = dict(ok=True, count=len(tweets), authors=authors[:5], cached=False)
    _cache[key] = (time.monotonic(), payload)
    return payload


# ── Рейтинг KOL'ов: колл = найденное упоминание CA; win = пик ≥1.5x от цены колла ──
import json  # noqa: E402
import pathlib  # noqa: E402

CALLS_PATH = pathlib.Path(__file__).resolve().parent / "outputs" / "kol_calls.json"
WIN_X = 1.5
_calls_mem: dict | None = None


def _calls() -> dict:
    global _calls_mem
    if _calls_mem is None:
        try:
            _calls_mem = json.loads(CALLS_PATH.read_text())
        except Exception:
            _calls_mem = {}
    return _calls_mem


def _save_calls():
    try:
        CALLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALLS_PATH.write_text(json.dumps(_calls(), ensure_ascii=False))
    except Exception:
        pass


def record_calls(ca: str, authors: list, price: float):
    """Зафиксировать коллы: автор × токен × цена в момент обнаружения (не дублируем)."""
    d = _calls()
    rec = d.setdefault(ca, dict(price0=price or 0.0, peak=price or 0.0,
                                ts=time.time(), authors=[]))
    known = {a["username"] for a in rec["authors"]}
    for a in authors:
        u = a.get("username")
        if u and u not in known:
            rec["authors"].append(dict(username=u, followers=a.get("followers", 0),
                                       price=price or 0.0, ts=time.time()))
            known.add(u)
    _save_calls()


def update_prices(prices: dict):
    """Подтянуть пики цен по токенам из очередного скана (для winrate коллов)."""
    d = _calls()
    changed = False
    for ca, p in prices.items():
        r = d.get(ca)
        if r and p and p > r.get("peak", 0.0):
            r["peak"] = p
            changed = True
    if changed:
        _save_calls()


def rating() -> list:
    """Пер-автор: сколько коллов, доля ≥1.5x (winrate), медианный пик-множитель."""
    by: dict[str, dict] = {}
    for r in _calls().values():
        for a in r["authors"]:
            p0 = a.get("price") or r.get("price0") or 0.0
            mult = (r.get("peak", 0.0) / p0) if p0 > 0 else 0.0
            st = by.setdefault(a["username"], dict(username=a["username"],
                                                   calls=0, wins=0, mults=[]))
            st["calls"] += 1
            st["mults"].append(round(mult, 2))
            if mult >= WIN_X:
                st["wins"] += 1
    out = []
    for st in by.values():
        m = sorted(st["mults"])
        out.append(dict(username=st["username"], calls=st["calls"],
                        winrate=round(st["wins"] / st["calls"], 2) if st["calls"] else 0.0,
                        median_x=m[len(m) // 2] if m else 0.0))
    out.sort(key=lambda x: (-x["winrate"], -x["calls"]))
    return out
