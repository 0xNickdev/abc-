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
import time

import httpx

# Аудит 28.09: quote-api.jup.ag/v6 не резолвится → Phantom/N3 не торговали вовсе.
JUP_QUOTE = os.getenv("ABC_JUP_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
JUP_SWAP = os.getenv("ABC_JUP_SWAP_URL", "https://lite-api.jup.ag/swap/v1/swap")
SOL_MINT = "So11111111111111111111111111111111111111112"
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TIMEOUT = 15.0
# Аудит 10.10 (LIVE): pump.fun-токены двигаются на проценты за секунду — 1-1.5% слиппеджа
# = постоянные отказы, а стоп, который не исполнился, хуже стопа с проскальзыванием.
BUY_SLIPPAGE_BPS = int(os.getenv("ABC_BUY_SLIPPAGE_BPS", "500") or 500)      # 5% на вход
SELL_SLIPPAGE_BPS = int(os.getenv("ABC_SELL_SLIPPAGE_BPS", "1500") or 1500)  # 15% на выход
MAX_PRIORITY_LAMPORTS = int(os.getenv("ABC_MAX_PRIORITY_LAMPORTS", "200000") or 200000)  # ≤0.0002 SOL
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EHFQwjdHWDvcKFMGSJAuS"
# расширения Token-2022, при которых продать можно не всё/не всегда — вход блокируем
BAD_T22_EXT = {"transferFeeConfig", "permanentDelegate", "transferHook", "nonTransferable",
               "defaultAccountState", "pausableConfig"}


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
        # «auto» без потолка мог съесть позицию на 0.05 SOL в горячем блоке
        prioritizationFeeLamports={"priorityLevelWithMaxLamports": {
            "maxLamports": MAX_PRIORITY_LAMPORTS, "priorityLevel": "high"}}), timeout=TIMEOUT)
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


def build_buy(user_pubkey: str, ca: str, size_sol: float, slippage_bps: int | None = None) -> dict:
    """SOL → токен. Возвращает {tx(b64), out_amount(сырой int токена), price_impact}."""
    q = quote(SOL_MINT, ca, int(size_sol * 1e9), slippage_bps or BUY_SLIPPAGE_BPS)
    return dict(tx=swap_tx(q, user_pubkey), out_amount=int(q.get("outAmount", 0)),
                price_impact=float(q.get("priceImpactPct", 0) or 0))


def build_sell(user_pubkey: str, ca: str, fraction: float = 1.0,
               token_amount: int = 0, slippage_bps: int | None = None) -> dict:
    """Токен → SOL. Количество — от ЖИВОГО баланса кошелька (аудит 10.10: сохранённый
    quote-объём больше реального из-за слиппеджа/частичных TP → preflight отклонял каждую
    продажу, стоп не исполнялся никогда). token_amount позиции — лишь верхняя граница."""
    live = token_balance(user_pubkey, ca)
    amt = min(live, token_amount) if token_amount else live
    amt = int(amt * max(0.0, min(1.0, fraction)))
    slippage_bps = slippage_bps or SELL_SLIPPAGE_BPS
    if amt <= 0:
        raise RuntimeError("нулевой баланс токена — нечего продавать")
    q = quote(ca, SOL_MINT, amt, slippage_bps)
    return dict(tx=swap_tx(q, user_pubkey), out_amount=int(q.get("outAmount", 0)),
                sold_amount=amt, price_impact=float(q.get("priceImpactPct", 0) or 0))


def _rpc(method: str, params: list) -> dict:
    r = httpx.post(RPC_URL, json=dict(jsonrpc="2.0", id=1, method=method, params=params),
                   timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"{method}: {j['error'].get('message', j['error'])}")
    return j.get("result")


def confirm_fill(sig: str, owner: str, mint: str, timeout_s: float = 45.0) -> dict:
    """Дождаться подтверждения tx и вернуть ФАКТИЧЕСКОЕ исполнение для owner:
    {ok, err, sol_delta (SOL, со знаком, вкл. комиссии/ренту), token_delta_raw, decimals, fee}.
    Аудит 10.10: позиции/PnL писались по quote и по факту «подпись есть», а не «tx прошла» —
    упавшая продажа удаляла позицию при токенах в кошельке."""
    deadline = time.monotonic() + timeout_s
    status = None
    while time.monotonic() < deadline:
        st = (_rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {})
        status = (st.get("value") or [None])[0]
        if status and status.get("err") is not None:
            return dict(ok=False, err=f"tx failed: {status['err']}")
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            break
        time.sleep(1.0)
    else:
        return dict(ok=False, err="not confirmed in time (dropped/expired)")
    tx = None
    for _ in range(5):                                # getTransaction может отставать от статуса
        tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                           "maxSupportedTransactionVersion": 0}])
        if tx:
            break
        time.sleep(1.0)
    if not tx:
        return dict(ok=False, err="confirmed but transaction not retrievable")
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return dict(ok=False, err=f"tx failed: {meta['err']}")
    keys = [(k.get("pubkey") if isinstance(k, dict) else k)
            for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys", [])]
    sol_delta = 0.0
    if owner in keys:
        i = keys.index(owner)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9

    def _tok(bals):
        tot, dec = 0, None
        for b in bals or []:
            if b.get("owner") == owner and b.get("mint") == mint:
                ui = b.get("uiTokenAmount") or {}
                tot += int(ui.get("amount") or 0)
                dec = ui.get("decimals", dec)
        return tot, dec
    pre, d1 = _tok(meta.get("preTokenBalances"))
    post, d2 = _tok(meta.get("postTokenBalances"))
    return dict(ok=True, err=None, sol_delta=round(sol_delta, 9), token_delta_raw=post - pre,
                decimals=d2 if d2 is not None else d1, fee=(meta.get("fee") or 0) / 1e9,
                signer=keys[0] if keys else None)


def mint_risk(mint: str) -> str | None:
    """Блок-причина для РЕАЛЬНОГО входа по данным mint-аккаунта (None — ок): живая freeze/mint
    authority (могут заморозить/допечатать — продать не дадут) или опасные расширения Token-2022
    (комиссия за перевод, постоянный делегат, hook, непереводимость). Аудит 10.10 B9."""
    acc = (_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}).get("value")
    if not acc:
        return "mint account not found"
    info = (((acc.get("data") or {}).get("parsed") or {}).get("info")) or {}
    if info.get("freezeAuthority"):
        return "freeze authority active"
    if info.get("mintAuthority"):
        return "mint authority active"
    if acc.get("owner") == TOKEN_2022:
        bad = {x.get("extension") for x in info.get("extensions") or []} & BAD_T22_EXT
        if bad:
            return "token-2022 extension: " + ",".join(sorted(bad))
    return None
