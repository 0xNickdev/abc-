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
LEAD_FLAG_MIN = 30.0   # (4): официала считаем «опередили» только если лид ≥ этого (мин), иначе шум ботов
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


MEMORY_LOL = "https://api.memory.lol/v1/tw/"
_hist_cache: dict[str, tuple] = {}


def handle_history(username: str) -> dict:
    """История хендлов из memory.lol (БЕСПЛАТНО, без ключа): прошлые screen names аккаунта.
    Много ренеймов = красный флаг (ферма перекатывает аудиторию / прячет запуск). Кэш TTL.
    {} при сбое; ok=True с пустым списком, если аккаунт не найден (нет истории ренеймов)."""
    username = (username or "").lstrip("@").strip()
    if not username:
        return {}
    hit = _hist_cache.get(username)
    if hit and time.monotonic() - hit[0] < TTL:
        return dict(hit[1], cached=True)
    try:
        r = httpx.get(MEMORY_LOL + username, timeout=10.0)
        if r.status_code == 404:
            payload = dict(ok=True, names=[], renames=0, red_flag=False, cached=False)
            _hist_cache[username] = (time.monotonic(), payload)
            return payload
        r.raise_for_status()
        j = r.json() or {}
    except Exception:
        return {}
    names: list = []
    for acc in (j.get("accounts") or []):
        sn = acc.get("screen_names")
        if isinstance(sn, dict):
            names += list(sn.keys())
        elif isinstance(sn, list):
            names += [(x.get("screen_name") if isinstance(x, dict) else x) for x in sn]
    uniq = [n for n in dict.fromkeys(names) if n]
    payload = dict(ok=True, names=uniq[:10], renames=max(0, len(uniq) - 1),
                   red_flag=len(uniq) >= 4, cached=False)          # 4+ разных хендлов = ферма-флаг
    _hist_cache[username] = (time.monotonic(), payload)
    return payload


def _parse_ts(s) -> float | None:
    """created_at → epoch для сравнения; None если формат не распознан."""
    if not s:
        return None
    import datetime
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(str(s), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _tweet_rows(payload: dict) -> list:
    node = payload.get("tweets") or payload.get("data") or payload.get("list") or payload
    if isinstance(node, list):
        return node
    return (node.get("tweets") if isinstance(node, dict) else []) or []


def ca_timeline(ca: str, official: str = "") -> dict:
    """Соц-таймлайн контракта через getxapi advanced_search: кто и когда постил CA.
    (2) сколько РАЗНЫХ авторов/сообществ + было ли раннее упоминание;
    (4) если самый ранний пост НЕ от официального хендла (или раньше него) → red_flag
        («CA слили до официалов» = копия/инсайд/не тот твиттер). {} при отсутствии ключа/сбое."""
    api_key = key()
    if not api_key:
        return dict(ok=False, detail="GETXAPI_KEY не задан")
    ca = (ca or "").strip()
    official = (official or "").lstrip("@").strip().lower()
    if not ca:
        return {}
    ck = ("tl", hash(api_key) & 0xFFFF, ca)
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < TTL:
        return dict(hit[1], cached=True)
    try:
        j = _get("/twitter/tweet/advanced_search", {"q": ca, "product": "Latest"}, api_key)
        if j.get("_err"):
            return dict(ok=False, detail=f"getxapi: {j['_err']}")
    except Exception as e:
        return dict(ok=False, detail=f"getxapi: {e}")
    posts = []                                                     # (ts, author)
    for tw in _tweet_rows(j):
        if not isinstance(tw, dict):
            continue
        u = tw.get("author") or tw.get("user") or {}
        name = (u.get("userName") or u.get("username") or u.get("screen_name")
                or tw.get("userName") or tw.get("username") or "").lstrip("@").lower()
        if not name:
            continue
        ts = _parse_ts(tw.get("createdAt") or tw.get("created_at") or tw.get("time"))
        posts.append((ts if ts is not None else 9e18, name))      # без ts → в конец (не «первый»)
    if not posts:
        payload = dict(ok=True, mentions=0, communities=0, first_author=None,
                       posted_before_official=False, red_flag=False, cached=False)
        _cache[ck] = (time.monotonic(), payload)
        return payload
    posts.sort(key=lambda p: p[0])
    first_ts, first_author = posts[0]
    have_ts = first_ts < 9e18                                      # реальные timestamp'ы распарсились
    off_ts = min([p[0] for p in posts if p[1] == official], default=None) if official else None
    # lead: на сколько МИНУТ первое упоминание опередило официала (None если официал не постил/нет ts)
    lead_min = round((off_ts - first_ts) / 60.0, 1) if (have_ts and off_ts is not None) else None
    # (4) red_flag — НЕ на «опередили на секунды» (боты/быстрые коллеры это делают всегда), а только:
    #   официал ВООБЩЕ не постил свой CA (а ≥3 других постили) ИЛИ его опередили СУЩЕСТВЕННО (≥LEAD_FLAG_MIN).
    posted_before = False
    if have_ts and official and first_author != official:
        if off_ts is None:
            posted_before = len(posts) >= 3
        else:
            posted_before = (off_ts - first_ts) >= LEAD_FLAG_MIN * 60
    payload = dict(ok=True, mentions=len(posts), communities=len(set(a for _, a in posts)),
                   first_author=first_author, first_by_official=(first_author == official),
                   lead_min=lead_min, official_posted=(off_ts is not None),
                   posted_before_official=posted_before, red_flag=posted_before, cached=False)
    _cache[ck] = (time.monotonic(), payload)
    return payload
