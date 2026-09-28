"""Session-кошелёк для N3-автопилота (Этап 7): делегирование с ограниченным риском.

Модель: у юзера появляется отдельный «торговый» кошелёк, ключ которого генерирует и
держит СЕРВЕР (per-user, chmod 600). Юзер переводит на него небольшую сумму с основного
Phantom — и только этой суммой бот может распоряжаться без кликов. Основной кошелёк
сервер по-прежнему никогда не видит.

Честно про trade-offs:
  • это НЕ полный non-custodial: сессионный ключ лежит на сервере. Риск ограничен
    балансом session-кошелька (пополняешь на сколько готов доверить боту);
  • вывести остаток можно в любой момент (/api/session-wallet/withdraw → на основной);
  • реальная отправка дополнительно заперта ENABLE_LIVE_TRADING (иначе paper-учёт).

Подпись — solders (официальные Rust-биндинги Solana): VersionedTransaction от Jupiter
подписывается сессионным Keypair и уходит в RPC. Тесты мокают сеть целиком.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import execution  # RPC_URL / TIMEOUT общие с построением tx

LAMPORTS = 1_000_000_000


def _load_or_create(path: pathlib.Path) -> Keypair:
    """Ключ per-user: outputs/users/<pk>/session_key.json (chmod 600). Нет → создать."""
    if path.exists():
        # Аудит 28.09: битый файл раньше молча перезаписывался НОВЫМ ключом → средства на
        # старом адресе терялись навсегда. Теперь — ошибка, файл не трогаем.
        try:
            return Keypair.from_bytes(bytes(json.loads(path.read_text())))
        except Exception as e:
            raise RuntimeError(f"session key file unreadable, refusing to overwrite: {path.name}") from e
    kp = Keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(list(bytes(kp))))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return kp


def keypair_for(path: pathlib.Path) -> Keypair:
    return _load_or_create(path)


def balance_sol(pubkey: str) -> float:
    r = httpx.post(execution.RPC_URL, json=dict(
        jsonrpc="2.0", id=1, method="getBalance", params=[pubkey]),
        timeout=execution.TIMEOUT)
    r.raise_for_status()
    return round((r.json().get("result", {}) or {}).get("value", 0) / LAMPORTS, 6)


def sign_and_send(tx_b64: str, kp: Keypair) -> str:
    """Подписать VersionedTransaction (b64 от Jupiter) сессионным ключом и отправить в RPC.
    Возвращает tx-подпись (hash)."""
    vtx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(vtx.message, [kp])     # конструктор с keypair = подпись
    raw = base64.b64encode(bytes(signed)).decode()
    r = httpx.post(execution.RPC_URL, json=dict(
        jsonrpc="2.0", id=1, method="sendTransaction",
        params=[raw, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}]),
        timeout=execution.TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"sendTransaction: {j['error'].get('message', j['error'])}")
    return j["result"]


def withdraw_all(kp: Keypair, to_pubkey: str, keep_lamports: int = 5000) -> str:
    """Вернуть остаток SOL с session-кошелька на основной (минус комиссия)."""
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    bal = int(balance_sol(str(kp.pubkey())) * LAMPORTS)
    amount = bal - keep_lamports
    if amount <= 0:
        raise RuntimeError("на session-кошельке нечего выводить")
    r = httpx.post(execution.RPC_URL, json=dict(
        jsonrpc="2.0", id=1, method="getLatestBlockhash", params=[]),
        timeout=execution.TIMEOUT)
    r.raise_for_status()
    bh = Hash.from_string(r.json()["result"]["value"]["blockhash"])
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(),
                                 to_pubkey=Pubkey.from_string(to_pubkey), lamports=amount))
    tx = Transaction([kp], Message([ix], kp.pubkey()), bh)
    raw = base64.b64encode(bytes(tx)).decode()
    r2 = httpx.post(execution.RPC_URL, json=dict(
        jsonrpc="2.0", id=1, method="sendTransaction",
        params=[raw, {"encoding": "base64"}]), timeout=execution.TIMEOUT)
    r2.raise_for_status()
    j = r2.json()
    if j.get("error"):
        raise RuntimeError(f"withdraw: {j['error'].get('message', j['error'])}")
    return j["result"]
