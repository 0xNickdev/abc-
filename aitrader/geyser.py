"""Shyft Yellowstone gRPC (Geyser): цены позиций из ончейн-резервов в реальном времени.

Зачем (прод 10.10): цены свежих pump.fun-токенов (ещё на bonding curve, без пары на
DexScreener) брались опросом RPC Helius → ключ упёрся в лимит 429, стопы ослепли; плюс
смешение источников (GMGN на входе, кривая/DexScreener на выходе) рисовало фальшивые ×45/×94.

Здесь: один долгоживущий gRPC-стрим на ноду Shyft (Лондон, ~3 мс от VPS). Подписка на
аккаунты bonding curve наших позиций → каждое изменение резервов приходит за десятки мс →
цена = (virtual_sol/1e9)/(virtual_tok/1e6) — та же формула, что pumpcurve, без единиц-путаницы.
Состав подписки меняется на лету (новый SubscribeRequest в тот же стрим). Сбой → бэкофф,
watcher живёт на прежних источниках. Env: SHYFT_GRPC_URL, SHYFT_GRPC_TOKEN; ABC_GEYSER=0 выкл.
"""
from __future__ import annotations

import os
import pathlib
import queue
import sys
import threading
import time

import pumpcurve

_PROTO_DIR = pathlib.Path(__file__).resolve().parent / "ysproto"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))   # сгенерированные стабы импортят друг друга top-level

PING_S = 10.0           # Shyft/Yellowstone рвут молчащий стрим — пингуем
FRESH_S = 30.0          # цена старше — не считаем «живой» (кривая без сделок не меняется,
                        # но и не врёт: 30с без обновления = просто нет сделок)


def _url() -> str:
    return os.getenv("SHYFT_GRPC_URL", "").strip()


def _token() -> str:
    return os.getenv("SHYFT_GRPC_TOKEN", "").strip()


def enabled() -> bool:
    return bool(_url() and _token()) and os.getenv("ABC_GEYSER", "1").strip().lower() not in ("0", "false", "off", "no")


def _target(url: str) -> str:
    u = url.split("://", 1)[-1].rstrip("/")
    return u if ":" in u else u + ":443"


def parse_curve_bytes(raw: bytes) -> tuple[float, bool]:
    """(цена SOL/токен, complete) из сырых байт аккаунта кривой (как pumpcurve._parse_curve)."""
    try:
        vtok = int.from_bytes(raw[8:16], "little")
        vsol = int.from_bytes(raw[16:24], "little")
        complete = len(raw) > 48 and raw[48] == 1
        if vtok <= 0:
            return 0.0, complete
        return (vsol / 1e9) / (vtok / 1e6), complete
    except Exception:
        return 0.0, False


class GeyserFeed:
    def __init__(self, on_update=None):
        self._on_update = on_update or (lambda mint: None)
        self._lock = threading.Lock()
        self._want: dict[str, str] = {}               # curve PDA -> mint
        self._prices: dict[str, tuple[float, float, bool]] = {}   # mint -> (px_sol, wall_ts, complete)
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = dict(state="off", updates=0, reconnects=0, subscribed=0, error="", last_update_ago_s=None)
        self._last_update = 0.0

    # ── публичное ─────────────────────────────────────────────────────────
    def start(self):
        if self._thread is None and enabled():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put(None)

    def set_mints(self, mints: list[str]):
        """Состав подписки = pump-токены позиций. Изменился → новый запрос в живой стрим."""
        want = {}
        for m in mints:
            if not m or not str(m).endswith("pump"):
                continue                                # кривая есть только у pump.fun
            try:
                want[pumpcurve.curve_address(m)] = m
            except Exception:
                continue
        with self._lock:
            if want == self._want:
                return
            self._want = want
            for m in list(self._prices):
                if m not in want.values():
                    self._prices.pop(m, None)
        self._q.put("resub")

    def prices_usd(self, mints: list[str], max_age_s: float = FRESH_S) -> dict[str, float]:
        """mint → USD по последнему обновлению кривой (не старше max_age_s, кривая не завершена)."""
        rate = pumpcurve.sol_usd()
        if rate <= 0:
            return {}
        now = time.time()
        out = {}
        with self._lock:
            for m in mints:
                hit = self._prices.get(m)
                if hit and not hit[2] and hit[0] > 0 and now - hit[1] <= max_age_s:
                    out[m] = round(hit[0] * rate, 12)
        return out

    def snapshot(self) -> dict:
        s = dict(self.stats)
        s["subscribed"] = len(self._want)
        s["last_update_ago_s"] = round(time.time() - self._last_update) if self._last_update else None
        return s

    # ── поток ─────────────────────────────────────────────────────────────
    def _request(self):
        import geyser_pb2 as pb
        with self._lock:
            accs = sorted(self._want)
        req = pb.SubscribeRequest(commitment=pb.CommitmentLevel.PROCESSED)
        if accs:
            req.accounts["positions"].account.extend(accs)
        return req

    def _requests(self):
        import geyser_pb2 as pb
        yield self._request()
        ping_id = 0
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=PING_S)
            except queue.Empty:
                ping_id += 1
                yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping_id))
                continue
            if item is None:
                return
            yield self._request()

    def _handle(self, upd):
        if not upd.HasField("account"):
            return
        import base58
        info = upd.account.account
        pda = base58.b58encode(bytes(info.pubkey)).decode()
        with self._lock:
            mint = self._want.get(pda)
        if not mint:
            return
        px, complete = parse_curve_bytes(bytes(info.data))
        now = time.time()
        with self._lock:
            self._prices[mint] = (px, now, complete)
        self._last_update = now
        self.stats["updates"] += 1
        try:
            self._on_update(mint)
        except Exception:
            pass

    def _loop(self):
        import geyser_pb2_grpc as pbg
        import grpc
        backoff = 0.0
        while not self._stop.is_set():
            try:
                creds = grpc.composite_channel_credentials(
                    grpc.ssl_channel_credentials(),
                    grpc.metadata_call_credentials(
                        lambda ctx, cb: cb((("x-token", _token()),), None)))
                with grpc.secure_channel(_target(_url()), creds, options=[
                        ("grpc.keepalive_time_ms", 20000),
                        ("grpc.max_receive_message_length", 64 * 1024 * 1024)]) as ch:
                    stub = pbg.GeyserStub(ch)
                    self.stats["state"] = "streaming"
                    for upd in stub.Subscribe(self._requests()):
                        backoff = 0.0
                        self._handle(upd)
                        if self._stop.is_set():
                            break
            except Exception as e:
                self.stats["state"] = "reconnecting"
                self.stats["reconnects"] += 1
                self.stats["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                backoff = min(120.0, backoff * 2) if backoff else 2.0
                self._stop.wait(backoff)
                # новый стрим начнёт с полного запроса — очередь «resub» больше не нужна
                while not self._q.empty():
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        break


FEED = GeyserFeed()
