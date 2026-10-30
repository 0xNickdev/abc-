"""Фильтр входа по модели ZOOAI AGENCY (zooaiagency.com, свой второй проект юзера, 30.10):
калиброванные шансы на 34 183 запусках pump.fun, AUC 0.89 — то, чего не хватало нашему входу
(отсев дев-дампа/рага ДО покупки). Публичный бесплатный API /api/check/<mint>.

odds.run — шанс, что капа пробьёт 85 SOL после 5-й минуты (среднее ~2.7%); vsAverage — во сколько
раз выше среднего; devDumps — шанс, что дев, ещё не сливший, сольёт позже; verdict/proceed — вердикт.
Наш гейт: пропустить, если шанс роста не ниже порога и дев-дамп не выше порога; явный avoid режем.
Fail-open: API недоступен → не блокируем (бот не должен вставать из-за чужого сервиса)."""
from __future__ import annotations

import os
import time

import httpx

BASE = os.getenv("ABC_ZOOAI_BASE", "https://zooaiagency.com/api/check").rstrip("/")
MODE = os.getenv("ABC_ZOOAI", "gate").strip().lower()                 # off | shadow | gate
MIN_RUN = float(os.getenv("ABC_ZOOAI_MIN_RUN", "0.03") or 0)          # ниже шанса роста — не входим
MIN_VS_AVG = float(os.getenv("ABC_ZOOAI_MIN_VSAVG", "0") or 0)        # или: во сколько раз выше среднего
MAX_DEVDUMPS = float(os.getenv("ABC_ZOOAI_MAX_DEVDUMPS", "0.6") or 0)  # выше шанса дев-дампа — режем
# кабала/кошельки (facts+funding): финансёр-рагер, дев сливает серийно, бандлеры держат много
MAX_DUMPED48H = int(os.getenv("ABC_ZOOAI_MAX_DUMPED48H", "3") or 0)   # дев слил столько своих запусков за 48ч
MAX_BUNDLERS_HELD = float(os.getenv("ABC_ZOOAI_MAX_BUNDLERS_HELD", "30") or 0)  # % саплая у бандлеров сейчас
TTL_S = float(os.getenv("ABC_ZOOAI_TTL_S", "600") or 600)
TIMEOUT = float(os.getenv("ABC_ZOOAI_TIMEOUT", "6") or 6)

_cache: dict[str, tuple] = {}                                         # mint -> (ts, payload|None)
STATS = dict(calls=0, errors=0, avoid=0, low_run=0, dev_dump=0, pass_=0)


def enabled() -> bool:
    return MODE in ("shadow", "gate")


def _fnum(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def check(mint: str) -> dict | None:
    """Срез ZOOAI по токену (кэш TTL). None — сеть/парсинг не удались (fail-open)."""
    now = time.time()
    hit = _cache.get(mint)
    if hit and now - hit[0] < TTL_S:
        return hit[1]
    try:
        STATS["calls"] += 1
        r = httpx.get(f"{BASE}/{mint}", headers={"accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json() or {}
        odds = d.get("odds") or {}
        fa, fu = d.get("facts") or {}, d.get("funding") or {}
        # кабала: финансёр НЕ биржа и кормит других девов, которые сливают
        cabal = bool(fu.get("funder") and not fu.get("funderIsHot")
                     and (fu.get("otherCreators") or 0) > 0 and (fu.get("theirDumps48h") or 0) > 0)
        out = dict(verdict=d.get("verdict"), proceed=d.get("proceed"),
                   run=_fnum(odds.get("run")), vs_average=_fnum(odds.get("vsAverage")),
                   dev_dumps=_fnum(odds.get("devDumps")),
                   dumped48h=fa.get("creatorDumped48h"), dump_min=fa.get("creatorMedianDumpMin"),
                   bundlers_held=_fnum(fa.get("bundlersHeldPct")), funder_cabal=cabal,
                   funder_label=fu.get("funderLabel"), other_dumpers=fu.get("theirDumps48h"),
                   reasons=[str(x)[:120] for x in (d.get("reasons") or [])][:4])
    except Exception:
        STATS["errors"] += 1
        out = None
    _cache[mint] = (now, out)
    if len(_cache) > 5000:
        for k in [k for k, v in _cache.items() if now - v[0] > 2 * TTL_S]:
            _cache.pop(k, None)
    return out


def block_reason(z: dict | None) -> str | None:
    """Причина отказа по ZOOAI или None (пропустить). None-поля не режут (fail-open)."""
    if not z:
        return None
    if z.get("verdict") == "avoid" or z.get("proceed") is False:
        STATS["avoid"] += 1
        return "zooai avoid"
    run, vs = z.get("run"), z.get("vs_average")
    if run is not None and MIN_RUN > 0 and run < MIN_RUN and not (MIN_VS_AVG > 0 and vs and vs >= MIN_VS_AVG):
        STATS["low_run"] += 1
        return f"zooai run {run:.3f}"
    dd = z.get("dev_dumps")
    if dd is not None and MAX_DEVDUMPS > 0 and dd > MAX_DEVDUMPS:
        STATS["dev_dump"] += 1
        return f"zooai devDumps {dd:.2f}"
    if z.get("funder_cabal"):                                         # финансёр-рагер кормит сливщиков
        STATS["cabal"] = STATS.get("cabal", 0) + 1
        return "zooai cabal"
    d48 = z.get("dumped48h")
    if d48 is not None and MAX_DUMPED48H > 0 and d48 >= MAX_DUMPED48H:  # дев серийно сливает свои запуски
        STATS["serial_dump"] = STATS.get("serial_dump", 0) + 1
        return f"zooai dumped48h {d48}"
    bh = z.get("bundlers_held")
    if bh is not None and MAX_BUNDLERS_HELD > 0 and bh >= MAX_BUNDLERS_HELD:  # бандлеры держат крупную долю
        STATS["bundle"] = STATS.get("bundle", 0) + 1
        return f"zooai bundlers {bh:g}%"
    STATS["pass_"] += 1
    return None


def snapshot() -> dict:
    return dict(mode=MODE, base=BASE, min_run=MIN_RUN, max_devdumps=MAX_DEVDUMPS, **STATS)
