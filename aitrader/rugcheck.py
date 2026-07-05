"""Он-чейн риск холдеров: бандлеры / инсайдеры / danger-риски (из RugCheck free API).

БЕЛЫЙ ЛЕЙБЛ (правило юзера): наружу отдаём как СВОИ сигналы, имя источника в UI НЕ светим.
Бесплатно, без ключа: GET https://api.rugcheck.xyz/v1/tokens/{mint}/report. Кэш TTL, мягко
при сбое (сеть/404 → пусто/нейтрально, никогда не бросаем). Красным флагить НУЖНО.
"""
from __future__ import annotations

import time

import httpx

BASE = "https://api.rugcheck.xyz/v1/tokens/"
TTL = 300.0
_cache: dict[str, tuple] = {}


def check(mint: str) -> dict:
    """{ok, insiders, insider_networks, bundled, danger[], rugged, red_flag}. Без бренда источника."""
    mint = (mint or "").strip()
    if not mint:
        return {}
    hit = _cache.get(mint)
    if hit and time.monotonic() - hit[0] < TTL:
        return dict(hit[1], cached=True)
    try:
        r = httpx.get(BASE + mint + "/report", timeout=12.0)
        if r.status_code == 404:                       # нет отчёта → нейтрально
            payload = dict(ok=True, insiders=0, insider_networks=0, bundled=0,
                           danger=[], rugged=False, red_flag=False)
            _cache[mint] = (time.monotonic(), payload)
            return payload
        r.raise_for_status()
        d = r.json() or {}
    except Exception:
        return {}
    holders = d.get("topHolders") or []
    insiders = sum(1 for h in holders if isinstance(h, dict) and h.get("insider"))
    nets = d.get("insiderNetworks") or []
    bundled = sum(1 for n in nets if isinstance(n, dict) and "bundl" in str(n.get("type", "")).lower())
    danger = [x.get("name") for x in (d.get("risks") or [])
              if isinstance(x, dict) and x.get("level") == "danger"]
    rugged = bool(d.get("rugged"))
    red = rugged or bool(danger) or insiders >= 3 or bundled >= 1
    payload = dict(ok=True, insiders=insiders, insider_networks=len(nets),
                   bundled=bundled, danger=danger[:5], rugged=rugged, red_flag=red)
    _cache[mint] = (time.monotonic(), payload)
    return payload
