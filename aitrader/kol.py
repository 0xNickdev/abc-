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
