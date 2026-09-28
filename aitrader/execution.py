"""Non-custodial исполнение (Этап 5): сервер строит транзакцию, подписывает — браузер (Phantom).

Приватный ключ юзера НИКОГДА не попадает на сервер:
  1) /api/tx/build  → Jupiter v6 quote+swap → сериализованная НЕПОДПИСАННАЯ tx (base64);
  2) браузер: Phantom signAndSendTransaction (юзер видит и подтверждает каждую);
  3) /api/tx/confirm → запись позиции с реальным tx-хэшем (учёт/риск/логи).

Сервер здесь только «конструктор»: никаких средств не касается, слиппедж и размер
зажаты в app.py (max_per_trade_sol / slippage cap). Все вызовы — httpx с таймаутом,
ошибки сети наверх (endpoint переводит в 502), тесты мокают build_buy/build_sell.
"""
from __future__ import annotations

import os

import httpx

# Аудит 28.09: quote-api.jup.ag/v6 не резолвится → Phantom/N3 не торговали вовсе.
JUP_QUOTE = os.getenv("ABC_JUP_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
JUP_SWAP = os.getenv("ABC_JUP_SWAP_URL", "https://lite-api.jup.ag/swap/v1/swap")
SOL_MINT = "So11111111111111111111111111111111111111112"
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TIMEOUT = 15.0


def quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100) -> dict:
    """Jupiter quote: amount в минимальных единицах input-токена (lamports для SOL)."""
    r = httpx.get(JUP_QUOTE, params=dict(
        inputMint=input_mint, outputMint=output_mint, amount=str(amount),
        slippageBps=slippage_bps, swapMode="ExactIn"), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Jupiter quote: {j['error']}")
    return j


def swap_tx(quote_resp: dict, user_pubkey: str) -> str:
    """Jupiter swap → base64 сериализованная VersionedTransaction (неподписанная)."""
    r = httpx.post(JUP_SWAP, json=dict(
        quoteResponse=quote_resp, userPublicKey=user_pubkey,
        wrapAndUnwrapSol=True, dynamicComputeUnitLimit=True,
        prioritizationFeeLamports="auto"), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    tx = j.get("swapTransaction")
    if not tx:
        raise RuntimeError(f"Jupiter swap: {j.get('error') or 'нет swapTransaction'}")
    return tx


def token_balance(owner: str, mint: str) -> int:
    """Сырой баланс SPL-токена у владельца (сумма по токен-аккаунтам), через public RPC."""
    r = httpx.post(RPC_URL, json=dict(
        jsonrpc="2.0", id=1, method="getTokenAccountsByOwner",
        params=[owner, {"mint": mint}, {"encoding": "jsonParsed"}]), timeout=TIMEOUT)
    r.raise_for_status()
    total = 0
    for acc in (r.json().get("result", {}) or {}).get("value", []):
        try:
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
    return total


def build_buy(user_pubkey: str, ca: str, size_sol: float, slippage_bps: int = 100) -> dict:
    """SOL → токен. Возвращает {tx(b64), out_amount(сырой int токена), price_impact}."""
    q = quote(SOL_MINT, ca, int(size_sol * 1e9), slippage_bps)
    return dict(tx=swap_tx(q, user_pubkey), out_amount=int(q.get("outAmount", 0)),
                price_impact=float(q.get("priceImpactPct", 0) or 0))


def build_sell(user_pubkey: str, ca: str, fraction: float = 1.0,
               token_amount: int = 0, slippage_bps: int = 150) -> dict:
    """Токен → SOL. token_amount (сырой) берём из позиции; 0 → спросить RPC-баланс."""
    amt = token_amount or token_balance(user_pubkey, ca)
    amt = int(amt * max(0.0, min(1.0, fraction)))
    if amt <= 0:
        raise RuntimeError("нулевой баланс токена — нечего продавать")
    q = quote(ca, SOL_MINT, amt, slippage_bps)
    return dict(tx=swap_tx(q, user_pubkey), out_amount=int(q.get("outAmount", 0)),
                sold_amount=amt, price_impact=float(q.get("priceImpactPct", 0) or 0))
