"""Цена pump.fun-токена напрямую из аккаунта bonding curve (Helius/любой RPC).

Зачем: совсем свежие pump.fun-токены ещё не проиндексированы DexScreener, а GMGN
может быть в 429-бане → watcher слеп, PnL замерзает, стоп спит (наблюдали закрытия
−46…−62%). Bonding curve — первоисточник: цена читается из аккаунта с ПЕРВОЙ секунды
жизни токена, без индексаторов.

Механика: PDA(["bonding-curve", mint], PUMP_PROGRAM) → getMultipleAccounts (батч до 100)
→ в данных аккаунта (8 байт дискриминатор, дальше u64 LE): virtual_token_reserves,
virtual_sol_reserves, … , complete(bool, offset 48). Цена SOL/токен = (vsol/1e9)/(vtok/1e6);
у pump.fun токенов decimals=6 фиксированно. complete=True → кривая закрыта (мигрировал
на AMM) — тогда цену должен знать DexScreener, кривую пропускаем.

Единицы: наши entry/cur цены в USD → конвертируем через курс SOL/USD (Jupiter Price API,
кэш 60с; сбой → последний известный курс; курса нет вообще → честно ничего не отдаём,
замороженный стоп хуже, чем пропущенный тик).
"""
from __future__ import annotations

import base64
import os
import time

import httpx
from solders.pubkey import Pubkey

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_PRICE = "https://api.jup.ag/price/v2"
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TIMEOUT = 8.0

_sol_cache: tuple[float, float] = (0.0, 0.0)      # (monotonic_ts, usd)
SOL_TTL = 60.0


def curve_address(mint: str) -> str:
    """PDA bonding curve для минта (детерминированно, без сети)."""
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM)
    return str(pda)


def _parse_curve(data_b64: str) -> tuple[float, bool]:
    """(цена SOL/токен, complete) из данных аккаунта; (0, False) если не парсится."""
    try:
        raw = base64.b64decode(data_b64)
        vtok = int.from_bytes(raw[8:16], "little")     # virtual_token_reserves (decimals 6)
        vsol = int.from_bytes(raw[16:24], "little")    # virtual_sol_reserves (lamports)
        complete = len(raw) > 48 and raw[48] == 1
        if vtok <= 0:
            return 0.0, complete
        return (vsol / 1e9) / (vtok / 1e6), complete
    except Exception:
        return 0.0, False


def sol_usd() -> float:
    """Курс SOL/USD (Jupiter Price API, кэш 60с; сбой → последний известный)."""
    global _sol_cache
    ts, usd = _sol_cache
    now = time.monotonic()
    if usd > 0 and now - ts < SOL_TTL:
        return usd
    try:
        r = httpx.get(JUP_PRICE, params={"ids": SOL_MINT}, timeout=TIMEOUT)
        r.raise_for_status()
        px = float(((r.json().get("data") or {}).get(SOL_MINT) or {}).get("price") or 0.0)
        if px > 0:
            _sol_cache = (now, px)
            return px
    except Exception:
        pass
    return usd                                        # протухший лучше, чем ноль (SOL стабилен на минутах)


def curve_prices_sol(mints: list[str]) -> dict[str, float]:
    """Батч: mint → цена в SOL/токен по живой bonding curve. Закрытые кривые и
    несуществующие аккаунты пропускаются (там цену знает AMM/DexScreener)."""
    out: dict[str, float] = {}
    mints = [m for m in mints if m]
    for i in range(0, len(mints), 100):
        chunk, addrs = [], []
        for m in mints[i:i + 100]:
            try:
                addrs.append(curve_address(m))     # невалидный base58 → пропуск, не роняем батч
                chunk.append(m)
            except Exception:
                continue
        if not addrs:
            continue
        r = httpx.post(RPC_URL, json=dict(
            jsonrpc="2.0", id=1, method="getMultipleAccounts",
            params=[addrs, {"encoding": "base64", "commitment": "confirmed"}]),
            timeout=TIMEOUT)
        r.raise_for_status()
        vals = (r.json().get("result") or {}).get("value") or []
        for mint, acc in zip(chunk, vals):
            if not acc:
                continue
            data = (acc.get("data") or [None])[0]
            if not data:
                continue
            px, complete = _parse_curve(data)
            if px > 0 and not complete:
                out[mint] = px
    return out


TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def ata_address(owner: str, mint: str) -> str:
    """Associated Token Account кошелька для минта (детерминированно, без сети)."""
    pda, _ = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(TOKEN_PROGRAM), bytes(Pubkey.from_string(mint))],
        ATA_PROGRAM)
    return str(pda)


def token_amounts(atas: list[str]) -> dict[str, int]:
    """Сырые балансы SPL токен-аккаунтов батчом (amount = u64 LE по оффсету 64).
    Несуществующий аккаунт (продал всё и закрыл) → 0. Для Smart-Exit Mirror."""
    out: dict[str, int] = {}
    atas = [a for a in atas if a]
    for i in range(0, len(atas), 100):
        chunk = atas[i:i + 100]
        r = httpx.post(RPC_URL, json=dict(
            jsonrpc="2.0", id=1, method="getMultipleAccounts",
            params=[chunk, {"encoding": "base64", "commitment": "confirmed"}]),
            timeout=TIMEOUT)
        r.raise_for_status()
        vals = (r.json().get("result") or {}).get("value") or []
        for ata, acc in zip(chunk, vals):
            if not acc:
                out[ata] = 0                      # аккаунт закрыт → баланс 0 (слил всё)
                continue
            try:
                raw = base64.b64decode((acc.get("data") or [""])[0])
                out[ata] = int.from_bytes(raw[64:72], "little")
            except Exception:
                continue                          # не парсится → пропуск (не считаем нулём)
    return out


def usd_prices(mints: list[str]) -> dict[str, float]:
    """mint → цена в USD (кривая × курс SOL). Пусто, если курс SOL недоступен —
    не подмешиваем цены в неправильных единицах."""
    rate = sol_usd()
    if rate <= 0:
        return {}
    return {m: round(px * rate, 12) for m, px in curve_prices_sol(mints).items()}
