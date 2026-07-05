"""X (Twitter) reuse-детектор через getxapi.com — независимая проверка «кто стоит за токеном».

Идея юзера: один и тот же твиттер-хендл часто привязывают к РАЗНЫМ токенам (серийный
шиллер / ферма перекатывает аудиторию), а свежий аккаунт под запуском = красный флаг.
Здесь: по хендлу тянем его последние твиты → сколько РАЗНЫХ solana-контрактов он постил
(reuse), + возраст аккаунта и подписчики. Много разных CA или очень молодой аккаунт = флаг.

getxapi: base https://api.getxapi.com, auth `Authorization: Bearer <key>`, ~$0.001/запрос →
дёргаем ТОЛЬКО по кнопке/по требованию, кэш TTL, ключ оператора в env GETXAPI_KEY (не в код).
Ренейм-историю хендла getxapi НЕ отдаёт (нет эндпоинта) → возраст аккаунта как прокси.
"""
from __future__ import annotations

import os
import re
import time

import httpx

BASE = "https://api.getxapi.com"
TTL = 600.0
_cache: dict[tuple, tuple] = {}                       # (key_fp, ca, user) -> (ts, payload)

# base58 (без 0 O I l) 32–44 символа = solana pubkey/CA
_CA_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def key() -> str:
    return (os.getenv("GETXAPI_KEY") or os.getenv("X_DATA_API_KEY") or "").strip()


def _get(path: str, params: dict, api_key: str) -> dict:
    r = httpx.get(BASE + path, params=params,
                  headers={"Authorization": f"Bearer {api_key}"}, timeout=12.0)
    if r.status_code in (401, 403):
        return dict(_err="auth")
    if r.status_code == 429:
        return dict(_err="rate_limited")
    r.raise_for_status()
    return r.json() or {}


def _dig(d: dict, *names):
    """Первое непустое поле из возможных имён (getxapi/твиттер варьируют camelCase/snake_case)."""
    src = d.get("data") if isinstance(d.get("data"), dict) else d
    for n in names:
        v = (src or {}).get(n)
        if v not in (None, ""):
            return v
    return None


def _account_age_days(created) -> float | None:
    if not created:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            import datetime
            dt = datetime.datetime.strptime(str(created), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return round((datetime.datetime.now(datetime.timezone.utc) - dt).days, 1)
        except ValueError:
            continue
    return None


def _tweet_texts(payload: dict) -> list[str]:
    """Достать тексты твитов из ответа (форма варьирует: data/tweets/list)."""
    node = payload.get("data") or payload.get("tweets") or payload.get("list") or payload
    rows = node if isinstance(node, list) else (node.get("tweets") if isinstance(node, dict) else [])
    out = []
    for t in rows or []:
        if isinstance(t, dict):
            out.append(str(t.get("text") or t.get("full_text") or ""))
    return out


def reuse_check(ca: str, username: str) -> dict:
    """Reuse-профиль хендла: возраст/подписчики + сколько РАЗНЫХ CA он постил (кроме этого).
    ok=False + detail при отсутствии ключа/ошибке. Кэш TTL. Никогда не бросает наружу."""
    api_key = key()
    if not api_key:
        return dict(ok=False, detail="GETXAPI_KEY не задан")
    username = (username or "").lstrip("@").strip()
    if not username:
        return dict(ok=False, detail="нет хендла")
    ck = (hash(api_key) & 0xFFFF, (ca or "").strip(), username)
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < TTL:
        return dict(hit[1], cached=True)
    try:
        info = _get("/twitter/user/info", {"userName": username}, api_key)
        if info.get("_err"):
            return dict(ok=False, detail=f"getxapi: {info['_err']}")
        tweets = _get("/twitter/user/tweets", {"userName": username}, api_key)
    except Exception as e:
        return dict(ok=False, detail=f"getxapi: {e}")
    cas = set()
    for txt in _tweet_texts(tweets):
        cas.update(_CA_RE.findall(txt))
    cas.discard((ca or "").strip())
    followers = _dig(info, "followers", "followers_count", "followersCount") or 0
    age = _account_age_days(_dig(info, "createdAt", "created_at", "creation_date"))
    other = len(cas)
    # флаг: серийный постинг разных CA ИЛИ очень молодой аккаунт (созданный под запуск)
    red = (other >= 3) or (age is not None and age < 7)
    payload = dict(ok=True, username=username, followers=int(followers),
                   account_age_days=age, other_tokens=other,
                   other_sample=sorted(cas)[:5], renames=None,     # ренеймы getxapi не отдаёт
                   red_flag=bool(red), cached=False)
    _cache[ck] = (time.monotonic(), payload)
    return payload
