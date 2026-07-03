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

import httpx

import execution  # общий RPC_URL/TIMEOUT

GT_TRENDING = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


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

    # ── точечные запросы (покупка/мониторинг): без отдельного индекса, через поиск пула ──
    def _pair(self, addr: str) -> dict:
        r = httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                      timeout=execution.TIMEOUT)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
        return max(pairs, key=lambda p: _f((p.get("liquidity", {}) or {}).get("usd"))) if pairs else {}

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
                    burn_ratio=0.0, top10=0.0)

    def token_holders(self, addr: str) -> dict:
        return dict(bundler_ratio=0.0, dev_holding=0.0, top10_concentration=0.0)

    def portfolio_stats(self, wallet: str) -> dict:
        return dict(wallet=wallet, win_rate=0.0, realized_pnl_sol=0.0)

    def wallet_address(self) -> str:
        raise RuntimeError("DexAdapter — только данные; исполнение идёт через Phantom/session-кошелёк")

    def swap(self, **kw):
        raise RuntimeError("DexAdapter — только данные; swap здесь невозможен")
