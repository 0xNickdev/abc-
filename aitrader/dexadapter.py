"""Реальные данные без ключей: GeckoTerminal (трендовые пулы Solana) + Solana RPC.

Зачем: канонический источник (GMGN OpenAPI) ходит через утилиту gmgn-cli, которой нет
ни локально, ни на Railway. Этот адаптер даёт НАСТОЯЩИЙ рынок бесплатно:
  • market_trending → GeckoTerminal /networks/solana/trending_pools (цена, объём,
    ликвидность, moment 5m/1h, покупки/продажи, возраст пула);
  • безопасность → батч getAccountInfo в Solana RPC: mintAuthority/freezeAuthority
    (сданы или нет) — ключевые гейты Соланы;
  • чего у источника НЕТ: smart-money/KOL-каунтеры, bundler%, dev-холд, снайперы →
    отдаём нули и выставляем provides_consensus=False, чтобы воронка не резала всё
    подряд по консенсус-гейту (см. screen_once/hard_gates).

Включение: env DATA_SOURCE=dex (см. MarketLayer.adapter_for). Лимит GeckoTerminal
~30 req/мин — наш TTL-кэш трендинга (3 c) и батч-RPC укладываются с запасом.
"""
from __future__ import annotations

import datetime
import os
import re
import time

import httpx

import execution  # общий RPC_URL/TIMEOUT

_TWITTER_RE = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})", re.I)

GT_TRENDING = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"

# ── Top-10 концентрация держателей через Solana RPC (бесплатно, без ключей) ──
# opt-in: getTokenLargestAccounts — тяжёлый метод, жрёт RPC-кредиты. Выключено по
# умолчанию → top10 остаётся 0.0 (старое поведение: гейт top10 и escape-дифф инертны).
# Включает: ABC_TOP10_RPC=1 → оживает top10-гейт скрининга + escape-мониторинг持仓.
TOP10_RPC = os.getenv("ABC_TOP10_RPC", "").strip().lower() in ("1", "true", "yes", "on")
_TOP10_TTL = 120.0                                  # концентрация меняется медленно → кэш на mint
_top10_cache: dict[str, tuple[float, float]] = {}   # mint -> (monotonic_ts, share)


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _rpc_post(reqs: list[dict]) -> dict:
    """Батч JSON-RPC одним POST → {id: ответ}. Бросает при сетевом сбое (ловит вызывающий)."""
    r = httpx.post(execution.RPC_URL, json=reqs, timeout=execution.TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return {x.get("id"): x for x in j} if isinstance(j, list) else {}


def _share_top10(largest: list, supply: float) -> float:
    """Доля top-10 держателей, ИСКЛЮЧАЯ крупнейший аккаунт (эвристика: это LP-пул —
    у свежих мемов пул держит основную долю супплая, это ликвидность, а не риск
    концентрации). Грубо, но убирает главный ложняк (иначе КАЖДЫЙ токен выглядел бы
    «сверх-концентрированным» и гейт резал бы всё). Ошибается в сторону НЕ-флага."""
    if supply <= 0 or len(largest) < 2:
        return 0.0
    holders = largest[1:11]                         # пропускаем крупнейший (пул), берём следующие 10
    return round(min(1.0, sum(_f(a.get("uiAmount")) for a in holders) / supply), 4)


def _top10_cache_get(mint: str):
    hit = _top10_cache.get(mint)
    if hit and time.monotonic() - hit[0] < _TOP10_TTL:
        return hit[1]
    return None


def _top10_cache_put(mint: str, share: float):
    _top10_cache[mint] = (time.monotonic(), share)
    if len(_top10_cache) > 4000:                     # кап памяти
        _top10_cache.pop(next(iter(_top10_cache)))


def _top10_concentration(mint: str) -> float:
    """Top-10 концентрация одного mint (кэш TTL). 0.0 при выключенном флаге/сбое RPC."""
    if not TOP10_RPC or not mint:
        return 0.0
    c = _top10_cache_get(mint)
    if c is not None:
        return c
    try:
        by = _rpc_post([
            dict(jsonrpc="2.0", id="lg", method="getTokenLargestAccounts", params=[mint]),
            dict(jsonrpc="2.0", id="sup", method="getTokenSupply", params=[mint])])
        largest = (by["lg"]["result"]["value"]) or []
        supply = _f((by["sup"]["result"]["value"] or {}).get("uiAmount"))
        share = _share_top10(largest, supply)
    except Exception:
        return 0.0
    _top10_cache_put(mint, share)
    return share


# ── Свежие кошельки среди топ-холдеров (чистый он-чейн, НЕ GMGN-агрегаты) ──
# Идея юзера: GMGN-метрики врут → проверять НЕЗАВИСИМО «сколько свежих кошельков в токене».
# Свежий = мало транзакций ЗА ВСЮ жизнь (< _ESTABLISHED_SIGS) И первая активность < FRESH_AGE_H
# назад. Высокая доля свежих среди топ-холдеров = бандл/ферма на запуске (коши созданы под токен).
# opt-in ABC_FRESH_WALLETS_RPC=1 (по N холдеров × getSignaturesForAddress — платит RPC-кредитами).
FRESH_RPC = os.getenv("ABC_FRESH_WALLETS_RPC", "").strip().lower() in ("1", "true", "yes", "on")
FRESH_AGE_H = _f(os.getenv("ABC_FRESH_AGE_H"), 48.0) or 48.0
_ESTABLISHED_SIGS = 100                              # ≥ столько подписей → «старый» кош, не свежий
_FRESH_TTL = 180.0
_fresh_cache: dict[str, tuple[float, dict]] = {}


def fresh_wallet_count(mint: str, top_n: int = 20) -> dict:
    """{fresh, checked, ratio} по топ-N холдерам (крупнейший=пул исключаем). {} при выкл/сбое.
    3 RPC-раунда: largest → владельцы (getMultipleAccounts) → история подписей (батч)."""
    if not FRESH_RPC or not mint:
        return {}
    hit = _fresh_cache.get(mint)
    if hit and time.monotonic() - hit[0] < _FRESH_TTL:
        return hit[1]
    try:
        lg = _rpc_post([dict(jsonrpc="2.0", id="lg", method="getTokenLargestAccounts", params=[mint])])
        accts = ((lg["lg"]["result"]["value"]) or [])[1:top_n + 1]   # [0] = пул, пропускаем
        token_accts = [a["address"] for a in accts if a.get("address")]
        if not token_accts:
            return {}
        mi = _rpc_post([dict(jsonrpc="2.0", id="mi", method="getMultipleAccounts",
                             params=[token_accts, {"encoding": "jsonParsed"}])])
        owners = []
        for v in (mi["mi"]["result"]["value"] or []):
            try:
                owners.append(v["data"]["parsed"]["info"]["owner"])
            except (KeyError, TypeError):
                continue
        owners = list(dict.fromkeys(owners))            # уникальные (кош мог держать в неск. аккаунтах)
        if not owners:
            return {}
        by = _rpc_post([dict(jsonrpc="2.0", id=f"s{i}", method="getSignaturesForAddress",
                             params=[o, {"limit": _ESTABLISHED_SIGS}]) for i, o in enumerate(owners)])
        now = time.time()
        fresh = 0
        for i in range(len(owners)):
            try:
                sigs = by[f"s{i}"]["result"] or []
            except (KeyError, TypeError):
                continue
            if len(sigs) >= _ESTABLISHED_SIGS:          # много истории → старый
                continue
            times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
            if times and (now - min(times)) < FRESH_AGE_H * 3600:   # первая активность недавно
                fresh += 1
        out = dict(fresh=fresh, checked=len(owners),
                   ratio=round(fresh / len(owners), 3) if owners else 0.0)
    except Exception:
        return {}
    _fresh_cache[mint] = (time.monotonic(), out)
    if len(_fresh_cache) > 4000:
        _fresh_cache.pop(next(iter(_fresh_cache)))
    return out


def spot_pair(addr: str) -> dict:
    """Лучший (по ликвидности) sol-пул токена из DexScreener — бесплатно, без ключа."""
    r = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                  timeout=execution.TIMEOUT)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    return max(pairs, key=lambda p: _f((p.get("liquidity", {}) or {}).get("usd"))) if pairs else {}


def spot_price(addr: str) -> float:
    """Свежая цена по CA — fallback мониторинга позиций: стопы не должны замерзать,
    когда GMGN в 429-бане/таймауте или монета вылетела из хот-листа."""
    return _f(spot_pair(addr).get("priceUsd"))


def spot_prices(mints: list[str]) -> dict[str, float]:
    """Цены пачкой (DexScreener принимает до 30 CA через запятую) — real-time монитор
    позиций дёргает это раз в ~2с ОДНИМ запросом на все открытые позиции. По каждому
    минту берём пул с максимальной ликвидностью (как spot_pair)."""
    out: dict[str, float] = {}
    for i in range(0, len(mints), 30):
        chunk = [m for m in mints[i:i + 30] if m]
        if not chunk:
            continue
        r = httpx.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk),
                      timeout=8.0)
        r.raise_for_status()
        best: dict[str, float] = {}                      # mint -> liq лучшего пула
        for p in (r.json().get("pairs") or []):
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            px = _f(p.get("priceUsd"))
            liq = _f((p.get("liquidity", {}) or {}).get("usd"))
            if mint in chunk and px > 0 and liq >= best.get(mint, -1.0):
                best[mint] = liq
                out[mint] = px
    return out


def spot_quotes(mints: list[str]) -> dict[str, dict]:
    """Цена + живая капа пачкой (DexScreener отдаёт marketCap/fdv в тех же pairs).
    Отдельная функция, чтобы не трогать spot_prices — на нём живёт контур стопов."""
    out: dict[str, dict] = {}
    for i in range(0, len(mints), 30):
        chunk = [m for m in mints[i:i + 30] if m]
        if not chunk:
            continue
        r = httpx.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk),
                      timeout=8.0)
        r.raise_for_status()
        best: dict[str, float] = {}
        for p in (r.json().get("pairs") or []):
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            px = _f(p.get("priceUsd"))
            liq = _f((p.get("liquidity", {}) or {}).get("usd"))
            if mint in chunk and px > 0 and liq >= best.get(mint, -1.0):
                best[mint] = liq
                out[mint] = dict(price=px, mcap=(_f(p.get("marketCap")) or _f(p.get("fdv"))))
    return out


def token_twitter(address: str) -> str:
    """X-хендл токена из socials DexScreener (info.socials type=twitter) → для авто X-reuse.
    '' если нет соц-ссылок/ошибка. Бесплатно, без ключей."""
    try:
        r = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                      timeout=execution.TIMEOUT)
        r.raise_for_status()
        for p in (r.json().get("pairs") or []):
            for s in ((p.get("info") or {}).get("socials") or []):
                if str(s.get("type", "")).lower() == "twitter":
                    m = _TWITTER_RE.search(str(s.get("url", "")))
                    if m and m.group(1).lower() not in ("i", "intent", "share", "home"):
                        return m.group(1)
    except Exception:
        return ""
    return ""


class DexAdapter:
    """Тот же интерфейс, что у GMGN-адаптеров (только чтение; swap здесь не бывает)."""

    provides_consensus = False     # нет данных об умных деньгах → гейт консенсуса пропускаем

    def market_trending(self, cmd=None, **kw) -> list[dict]:
        r = httpx.get(GT_TRENDING, params={"page": 1},
                      headers={"accept": "application/json"}, timeout=execution.TIMEOUT)
        r.raise_for_status()
        pools = (r.json().get("data") or [])[:40]
        now = datetime.datetime.now(datetime.timezone.utc)
        rows, mints = [], []
        for p in pools:
            a = p.get("attributes", {}) or {}
            rel = ((p.get("relationships", {}) or {}).get("base_token", {}) or {}).get("data", {}) or {}
            mint = str(rel.get("id", "")).removeprefix("solana_")
            if not mint or mint.startswith("0x"):
                continue
            created = a.get("pool_created_at")
            try:
                age_min = max(0.0, (now - datetime.datetime.fromisoformat(
                    created.replace("Z", "+00:00"))).total_seconds() / 60)
            except (TypeError, ValueError, AttributeError):
                age_min = 0.0
            tx = (a.get("transactions", {}) or {}).get("h1", {}) or {}
            chg = a.get("price_change_percentage", {}) or {}
            rows.append(dict(
                address=mint,
                symbol=(a.get("name", "?").split(" / ")[0] or "?"),
                price=_f(a.get("base_token_price_usd")),
                market_cap=_f(a.get("market_cap_usd") or a.get("fdv_usd")),
                volume=_f((a.get("volume_usd", {}) or {}).get("h1")),
                liquidity=_f(a.get("reserve_in_usd")),
                price_change_percent1h=_f(chg.get("h1")),
                price_change_percent5m=_f(chg.get("m5")),
                buys=int(_f(tx.get("buys"))), sells=int(_f(tx.get("sells"))),
                swaps=int(_f(tx.get("buys")) + _f(tx.get("sells"))),
                creation_timestamp=(now - datetime.timedelta(minutes=age_min)).timestamp(),
                # безопасность заполняется батчем ниже; недоступные полю — нейтральные нули
                is_honeypot=0, burn_ratio=0.0, buy_tax=0.0, sell_tax=0.0, rug_ratio=0.0,
                bundler_rate=0.0, dev_team_hold_rate=0.0, top_10_holder_rate=0.0,
                smart_degen_count=0, renowned_count=0, sniper_count=0,
                renounced_mint=1, renounced_freeze_account=1))
            mints.append(mint)
        self._enrich_dexscreener(rows, mints)
        self._fill_authorities(rows, mints)
        self._fill_top10(rows, mints)
        return rows

    def _enrich_dexscreener(self, rows: list[dict], mints: list[str]):
        """GT в трендинге отдаёт m5/h1 нулями — короткие метрики берём одним батчем
        у DexScreener (до 30 адресов на запрос): моментум, покупки/продажи, ликвидность."""
        if not mints:
            return
        try:
            r = httpx.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(mints[:30]),
                          timeout=execution.TIMEOUT)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
        except Exception:
            return                       # DexScreener недоступен → остаёмся на данных GT
        best: dict[str, dict] = {}
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken", {}) or {}).get("address", "")
            liq = _f((p.get("liquidity", {}) or {}).get("usd"))
            if mint and liq >= _f((best.get(mint, {}).get("liquidity", {}) or {}).get("usd")):
                best[mint] = p
        for row in rows:
            p = best.get(row["address"])
            if not p:
                continue
            ch = p.get("priceChange", {}) or {}
            tx1 = (p.get("txns", {}) or {}).get("h1", {}) or {}
            row.update(
                price=_f(p.get("priceUsd"), row["price"]),
                market_cap=_f(p.get("marketCap") or p.get("fdv"), row["market_cap"]),
                volume=_f((p.get("volume", {}) or {}).get("h1"), row["volume"]),
                liquidity=_f((p.get("liquidity", {}) or {}).get("usd"), row["liquidity"]),
                price_change_percent1h=_f(ch.get("h1"), row["price_change_percent1h"]),
                price_change_percent5m=_f(ch.get("m5"), row["price_change_percent5m"]),
                buys=int(_f(tx1.get("buys"))), sells=int(_f(tx1.get("sells"))))
            row["swaps"] = row["buys"] + row["sells"]
            created_ms = p.get("pairCreatedAt")
            if created_ms:
                row["creation_timestamp"] = _f(created_ms) / 1000.0

    def _fill_authorities(self, rows: list[dict], mints: list[str]):
        """Один батч-запрос RPC: getAccountInfo(jsonParsed) на все mint'ы —
        mintAuthority/freezeAuthority null == права сданы (главные гейты Соланы)."""
        if not mints:
            return
        batch = [dict(jsonrpc="2.0", id=i, method="getAccountInfo",
                      params=[m, {"encoding": "jsonParsed"}]) for i, m in enumerate(mints)]
        try:
            r = httpx.post(execution.RPC_URL, json=batch, timeout=execution.TIMEOUT)
            r.raise_for_status()
            by_id = {j.get("id"): j for j in r.json()} if isinstance(r.json(), list) else {}
        except Exception:
            return                       # RPC недоступен → оставляем нейтральные значения
        for i, row in enumerate(rows):
            try:
                info = by_id[i]["result"]["value"]["data"]["parsed"]["info"]
                row["renounced_mint"] = 1 if info.get("mintAuthority") in (None, "") else 0
                row["renounced_freeze_account"] = 1 if info.get("freezeAuthority") in (None, "") else 0
            except (KeyError, TypeError):
                continue

    def _fill_top10(self, rows: list[dict], mints: list[str]):
        """Батч top-10 концентрации на трендинг → оживляет top10-гейт скрининга + escape
        для持仓, ещё стоящих в榜. Один POST: по 2 запроса (largest+supply) на mint, и только
        для протухших в кэше mint'ов (соседние сканы переиспользуют кэш → RPC-нагрузка низкая)."""
        if not TOP10_RPC or not mints:
            return
        idx_mint = {i: m for i, m in enumerate(mints) if _top10_cache_get(m) is None}
        if idx_mint:
            reqs = []
            for i, m in idx_mint.items():
                reqs.append(dict(jsonrpc="2.0", id=f"l{i}", method="getTokenLargestAccounts", params=[m]))
                reqs.append(dict(jsonrpc="2.0", id=f"s{i}", method="getTokenSupply", params=[m]))
            try:
                by = _rpc_post(reqs)
            except Exception:
                by = {}
            for i, m in idx_mint.items():
                try:
                    largest = by[f"l{i}"]["result"]["value"] or []
                    supply = _f((by[f"s{i}"]["result"]["value"] or {}).get("uiAmount"))
                    _top10_cache_put(m, _share_top10(largest, supply))
                except (KeyError, TypeError):
                    continue
        for row in rows:                            # проставить из кэша (свежие + только что записанные)
            c = _top10_cache_get(row["address"])
            if c is not None:
                row["top_10_holder_rate"] = c

    # ── точечные запросы (покупка/мониторинг): без отдельного индекса, через поиск пула ──
    def _pair(self, addr: str) -> dict:
        return spot_pair(addr)

    def token_info(self, addr: str) -> dict:
        p = self._pair(addr)
        return dict(address=addr, symbol=(p.get("baseToken", {}) or {}).get("symbol", addr[:6]),
                    price=_f(p.get("priceUsd")), market_cap=_f(p.get("marketCap")))

    def token_price(self, addr: str) -> float:
        return _f(self._pair(addr).get("priceUsd"))

    def token_security(self, addr: str) -> dict:
        rows = [dict(renounced_mint=1, renounced_freeze_account=1)]
        self._fill_authorities(rows, [addr])
        return dict(honeypot=False, renounced_mint=bool(rows[0]["renounced_mint"]),
                    renounced_freeze=bool(rows[0]["renounced_freeze_account"]),
                    burn_ratio=0.0, top10=_top10_concentration(addr))

    def token_holders(self, addr: str) -> dict:
        # bundler/dev-hold требуют GMGN-ключ; top10 берём бесплатно из RPC (opt-in ABC_TOP10_RPC)
        return dict(bundler_ratio=0.0, dev_holding=0.0,
                    top10_concentration=_top10_concentration(addr))

    def portfolio_stats(self, wallet: str) -> dict:
        return dict(wallet=wallet, win_rate=0.0, realized_pnl_sol=0.0)

    def wallet_address(self) -> str:
        raise RuntimeError("DexAdapter — только данные; исполнение идёт через Phantom/session-кошелёк")

    def swap(self, **kw):
        raise RuntimeError("DexAdapter — только данные; swap здесь невозможен")
