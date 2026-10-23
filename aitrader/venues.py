"""Рынок от $200k за пределами pump.fun: Raydium, Meteora, Orca, LaunchLab (16.10).

Парсить каждую площадку отдельно — десяток форматов пулов (Raydium AMM v4/CPMM/CLMM/LaunchLab,
Meteora DLMM/DAMM v1/v2/DBC, Orca Whirlpool) и недели работы. Вместо этого:
  1) раз в REFRESH_S GeckoTerminal даёт пулы этих площадок с парой к SOL, капой ≥ $200k,
     ликвидностью ≥ $15k и возрастом ≤ 7 дней → список наблюдения (до WATCH_CAP токенов);
  2) стрим Yellowstone подписывается на транзакции ЭТИХ токенов (любая площадка и маршрут,
     включая Jupiter) — нагрузка ограничена списком;
  3) pumpflow разбирает такие транзакции по изменению балансов подписанта (токен ↑ и SOL ↓ —
     покупка), без форматов площадок; цена = SOL/токен сделки.
Цена сверяется с DexScreener в learn.mid_scan, как и для PumpSwap. Env: ABC_VENUES=0 выкл.
"""
from __future__ import annotations

import os
import threading
import time

GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
DEXES = ("raydium", "raydium-clmm", "raydium-launchlab", "meteora", "meteora-damm-v2", "meteora-dbc",
         "orca", "letsbonk-fun", "stonkfun")
# 16.10 живая проверка: «топ по объёму 24ч» площадки — это старые крупные токены (2022–2024),
# фильтр «≤ 7 дней» их честно отсекал → 2 токена в списке. Берём то, где живут молодые $200k+:
# новые пулы и трендовые по всей сети (не pump) + свежие лаунчпады по объёму.
LISTS = ([f"/new_pools?page={p}" for p in range(1, 9)]
         + [f"/trending_pools?page={p}&duration={d}" for d in ("1h", "6h", "24h") for p in (1, 2, 3)]
         + [f"/dexes/{d}/pools?page={p}&sort=h24_volume_usd_desc"
            for d in ("raydium-launchlab", "letsbonk-fun", "meteora-dbc", "stonkfun") for p in (1, 2)])
SKIP_DEX = {"pumpswap", "pump-fun"}            # pump.fun/PumpSwap видит сам поток
PACE_S = 3.0
REFRESH_S = 300
WATCH_CAP = 200
EXPIRE_S = 1800                # пропал из выдачи на полчаса — снимаем с наблюдения
MIN_MCAP_USD = 200_000
MIN_LIQ_USD = 15_000
MAX_AGE_H = 168
SOL_MINT = "So11111111111111111111111111111111111111112"


def enabled() -> bool:
    return os.getenv("ABC_VENUES", "1").strip().lower() not in ("0", "false", "off", "no")


def parse_pools(payload: dict, dex: str | None = None, now: float | None = None) -> list[dict]:
    """Ответ GeckoTerminal → пулы-кандидаты (пара к SOL, капа/ликвидность/возраст)."""
    now = now or time.time()
    out = []
    for p in payload.get("data") or []:
        a, r = p.get("attributes") or {}, p.get("relationships") or {}
        pdex = dex or ((r.get("dex") or {}).get("data") or {}).get("id", "")
        if pdex in SKIP_DEX:
            continue
        base = ((r.get("base_token") or {}).get("data") or {}).get("id", "")
        quote = ((r.get("quote_token") or {}).get("data") or {}).get("id", "")
        if not base.startswith("solana_") or quote != "solana_" + SOL_MINT:
            continue
        mint = base[len("solana_"):]
        if mint == SOL_MINT:
            continue
        try:
            mcap = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
            liq = float(a.get("reserve_in_usd") or 0)
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
            price = float(a.get("base_token_price_usd") or 0)
        except (TypeError, ValueError):
            continue
        created = a.get("pool_created_at")
        age_h = None
        if created:
            try:
                from datetime import datetime
                age_h = (now - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 3600
            except ValueError:
                pass
        if mcap < MIN_MCAP_USD or liq < MIN_LIQ_USD or (age_h is not None and age_h > MAX_AGE_H):
            continue
        out.append(dict(mint=mint, pool=a.get("address"), dex=pdex, mcap=mcap, liq=liq, vol24=vol,
                        price=price, age_h=round(age_h, 2) if age_h is not None else None))
    return out


class VenueWatch:
    def __init__(self, fetch=None, on_change=None):
        self._fetch = fetch                         # (url) → json; тесты подменяют сеть
        self._on_change = on_change or (lambda mints: None)
        self.lock = threading.Lock()
        self.watch: dict[str, dict] = {}            # mint → инфо + seen
        self.stats = dict(state="off", refreshes=0, errors=0, watched=0, last_error=None)
        self._thread: threading.Thread | None = None

    def fetch(self, url: str) -> dict:
        if self._fetch:
            return self._fetch(url)
        import httpx
        r = httpx.get(url, timeout=15.0, headers={"Accept": "application/json"})
        if r.status_code == 429:
            raise RuntimeError("429")
        r.raise_for_status()
        return r.json()

    def refresh(self, now: float | None = None) -> bool:
        now = now or time.time()
        found: dict[str, dict] = {}
        for path in LISTS:
            dex = path.split("/dexes/")[1].split("/")[0] if "/dexes/" in path else None
            try:
                j = self.fetch(f"{GECKO}{path}{'&' if '?' in path else '?'}include=dex")
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{path}: {type(e).__name__}: {e}"[:160]
                if self._fetch is None:
                    time.sleep(30 if "429" in str(e) else PACE_S)
                continue
            for c in parse_pools(j, dex, now):
                if c["mint"] not in found or c["liq"] > found[c["mint"]]["liq"]:
                    found[c["mint"]] = c
            if self._fetch is None:
                time.sleep(PACE_S)                  # GeckoTerminal бесплатно: ~20–30 запросов/мин
        with self.lock:
            before = set(self.watch)
            for m, c in found.items():
                self.watch[m] = dict(c, seen=now)
            for m in [m for m, c in self.watch.items() if now - c["seen"] > EXPIRE_S]:
                del self.watch[m]
            if len(self.watch) > WATCH_CAP:
                keep = sorted(self.watch.items(), key=lambda kv: -kv[1]["vol24"])[:WATCH_CAP]
                self.watch = dict(keep)
            after = set(self.watch)
        self.stats["refreshes"] += 1
        self.stats["watched"] = len(after)
        if after != before:
            try:
                self._on_change(sorted(after))
            except Exception as e:
                self.stats["last_error"] = f"on_change {type(e).__name__}: {e}"[:160]
            return True
        return False

    def mints(self) -> list[str]:
        with self.lock:
            return list(self.watch)

    def info(self, mint: str) -> dict | None:
        with self.lock:
            return dict(self.watch[mint]) if mint in self.watch else None

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:                  # никогда не роняет сервер
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]
            time.sleep(REFRESH_S)

    def start(self):
        if self._thread is None and enabled():
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, name="venues", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        with self.lock:
            by = {}
            for c in self.watch.values():
                by[c["dex"]] = by.get(c["dex"], 0) + 1
        return dict(self.stats, by_dex=by)


def _on_change(mints: list[str]):
    import geyser
    import pumpflow
    pumpflow.FLOW.set_watch(mints)
    geyser.FEED.set_watch(mints)


WATCH = VenueWatch(on_change=_on_change)
