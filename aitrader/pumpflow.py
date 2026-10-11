"""Свой поток сделок pump.fun + PumpSwap из Yellowstone gRPC (Shyft) — замена скана GMGN.

Зачем (11.10): GMGN банит IP по лимиту, а единственный доказанный эдж — перевес покупок
(buy_ratio ≥ 0.65) — у GMGN это агрегат за час с задержкой. Здесь каждая сделка приходит
за десятки мс после исполнения: перевес/объём/уникальные покупатели в любом окне (30с…1ч),
наши tracked-кошельки прямо в потоке, снайперы блока создания, продажи дева. Охват — все
токены pump.fun с первой секунды, не топ-100; банов нет.

Источник — Anchor-события, которые программы пишут о каждой сделке:
  pump.fun   TradeEvent (mint, sol, tokens, is_buy, user, ts, резервы после сделки…),
             CreateEvent (name, symbol, mint, creator…), CompleteEvent (кривая заполнена);
  PumpSwap   BuyEvent/SellEvent (резервы пула, user, pool) — mint берём из счетов
             инструкции buy/sell того же tx (pool → base_mint кэшируется).
События ищем и в self-CPI инструкциях (emit_cpi: тег e445a52e51cb9a1d + дискриминатор),
и в логах «Program data:» (emit!) — какой путь у программы, не важно.

Без лишнего риска: транзакции идут тем же стримом geyser.FEED (второе соединение могло бы
упереться в лимит тарифа и выбить цены позиций → ослепить стопы). Стрим только кладёт
сырое обновление в ограниченную очередь; разбор — в своей нити; очередь полна → дроп
со счётчиком, цены позиций никогда не ждут. Каждое событие проверяется (mint/кривая/пул
есть в счетах tx, is_buy ∈ {0,1}, время правдоподобно) — несходящееся считается в rejects,
по нему видно, если программа сменила раскладку. Env: ABC_FLOW=0 выключить.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import os
import queue
import struct
import threading
import time

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SOL_MINT = "So11111111111111111111111111111111111111112"


def _disc(kind: str, name: str) -> bytes:
    return hashlib.sha256(f"{kind}:{name}".encode()).digest()[:8]


EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")      # anchor emit_cpi: sha256("anchor:event")[:8] LE
EV_TRADE = _disc("event", "TradeEvent")
EV_CREATE = _disc("event", "CreateEvent")
EV_COMPLETE = _disc("event", "CompleteEvent")
EV_AMM_BUY = _disc("event", "BuyEvent")
EV_AMM_SELL = _disc("event", "SellEvent")
AMM_SWAP_IX = {_disc("global", "buy"), _disc("global", "sell"), _disc("global", "buy_exact_quote_in")}

BUCKET_S = 10           # гранулярность окон до часа
KEEP_S = 3600           # горизонт агрегатов (= окно trending GMGN 1h — для сверки)
RECENT_S = 300          # сырые сделки (тонкие окна, снайперы) — последние 5 мин
RECENT_CAP = 3000
USERS_CAP = 5000
MAX_MINTS = 25000       # pump.fun ~15–30k новых токенов/сутки; живых в часе — меньше
SNIPER_SLOTS = 2        # покупка в слоте создания … +2 слота = снайпер/бандл
QUEUE_MAX = 20000
GAP_S = 30              # тишина дольше — считаем разрывом покрытия (pump.fun не молчит и секунды)
TOKEN_DECIMALS = 6      # pump.fun-токены всегда 6 знаков
PUMP_SUPPLY = 1_000_000_000


def enabled() -> bool:
    return os.getenv("ABC_FLOW", "1").strip().lower() not in ("0", "false", "off", "no")


# ── разбор байт ──────────────────────────────────────────────────────────────
_b58 = None


def b58(b: bytes) -> str:
    global _b58
    if _b58 is None:
        import base58
        _b58 = base58.b58encode
    return _b58(bytes(b)).decode()


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def _i64(b: bytes, o: int) -> int:
    return struct.unpack_from("<q", b, o)[0]


def _str(b: bytes, o: int) -> tuple[str, int]:
    n = struct.unpack_from("<I", b, o)[0]
    if n > 512:
        raise ValueError("string too long")
    s = bytes(b[o + 4:o + 4 + n])
    if len(s) != n:
        raise ValueError("short string")
    return s.decode("utf-8", "replace"), o + 4 + n


def _ts_ok(ts: int) -> bool:
    return abs(ts - time.time()) < 86400 * 2


def parse_trade(ev: bytes) -> dict:
    """pump.fun TradeEvent (без 8 байт дискриминатора). Хвост (комиссии, объёмы) не нужен."""
    mint = ev[0:32]
    is_buy = ev[48]
    if is_buy not in (0, 1):
        raise ValueError("is_buy not bool")
    ts = _i64(ev, 81)
    if not _ts_ok(ts):
        raise ValueError("bad timestamp")
    d = dict(mint=b58(mint), sol=_u64(ev, 32), tokens=_u64(ev, 40), is_buy=bool(is_buy),
             user=b58(ev[49:81]), ts=ts,
             vsol=_u64(ev, 89), vtok=_u64(ev, 97), real_sol=_u64(ev, 105), real_tok=_u64(ev, 113))
    if len(ev) >= 201:   # real_tok | fee_recipient(32) fee_bps fee | creator(32) @169
        d["creator"] = b58(ev[169:201])
    return d


def parse_create(ev: bytes) -> dict:
    name, o = _str(ev, 0)
    symbol, o = _str(ev, o)
    _uri, o = _str(ev, o)
    mint, curve, user, creator = ev[o:o + 32], ev[o + 32:o + 64], ev[o + 64:o + 96], ev[o + 96:o + 128]
    if len(creator) != 32:
        raise ValueError("short create")
    ts = _i64(ev, o + 128)
    if not _ts_ok(ts):
        raise ValueError("bad timestamp")
    return dict(name=name[:64], symbol=symbol[:32], mint=b58(mint), curve=b58(curve),
                user=b58(user), creator=b58(creator), ts=ts)


def parse_complete(ev: bytes) -> dict:
    ts = _i64(ev, 96)
    if not _ts_ok(ts):
        raise ValueError("bad timestamp")
    return dict(user=b58(ev[0:32]), mint=b58(ev[32:64]), curve=b58(ev[64:96]), ts=ts)


def parse_amm(ev: bytes, is_buy: bool) -> dict:
    """PumpSwap BuyEvent/SellEvent: 14×u64, затем pool, user. Резервы — ДО сделки."""
    ts = _i64(ev, 0)
    if not _ts_ok(ts):
        raise ValueError("bad timestamp")
    base_amt = _u64(ev, 8)
    pool_base, pool_quote = _u64(ev, 40), _u64(ev, 48)
    quote_amt = _u64(ev, 56)                 # buy: quote_amount_in; sell: quote_amount_out
    if len(ev) < 176:
        raise ValueError("short amm event")
    if is_buy:
        pb, pq = pool_base - base_amt, pool_quote + quote_amt
    else:
        pb, pq = pool_base + base_amt, pool_quote - quote_amt
    return dict(ts=ts, tokens=base_amt, sol=quote_amt, is_buy=is_buy,
                pool=b58(ev[112:144]), user=b58(ev[144:176]), pool_base=max(pb, 0), pool_quote=max(pq, 0))


# ── разбор транзакции Yellowstone ────────────────────────────────────────────
def iter_events(tx_info, pools: dict) -> tuple[list, int]:
    """SubscribeUpdateTransactionInfo → ([(kind, dict, keyset)], rejects). pools: pool → (base, quote)."""
    msg = tx_info.transaction.message
    meta = tx_info.meta
    keys = list(msg.account_keys) + list(meta.loaded_writable_addresses) + list(meta.loaded_readonly_addresses)
    keyset = {bytes(k) for k in keys}
    pump_k, amm_k = _PUMP_B, _AMM_B
    out, rejects, from_ix = [], 0, False

    def handle(prog: bytes, data: bytes, accts: bytes):
        nonlocal rejects, from_ix
        if prog == amm_k and data[:8] in AMM_SWAP_IX and len(accts) >= 5:
            try:
                pool, base, quote = keys[accts[0]], keys[accts[3]], keys[accts[4]]
                pools[b58(pool)] = (b58(base), b58(quote))
            except IndexError:
                rejects += 1
            return
        if data[:8] != EVENT_IX_TAG or prog not in (pump_k, amm_k):
            return
        from_ix = True
        r = _decode(data[8:], prog == amm_k)
        if r is None:
            return
        if r == "bad":
            rejects += 1
        else:
            out.append(r)

    def walk(ixs):
        for ix in ixs:
            pi = ix.program_id_index
            if pi < len(keys):
                handle(bytes(keys[pi]), bytes(ix.data), bytes(ix.accounts))

    inner = {ii.index: ii.instructions for ii in meta.inner_instructions}
    for i, ix in enumerate(msg.instructions):
        walk([ix])
        walk(inner.get(i, ()))
    if any(k == "amm" and d["pool"] not in pools for k, d in out):
        # пул не встретился в известных buy/sell (новые варианты инструкций PumpSwap):
        # пары берём из балансов tx — у пула ровно два токен-счёта (base + quote) с owner=pool
        owned: dict[str, set] = {}
        for tb in list(meta.pre_token_balances) + list(meta.post_token_balances):
            if tb.owner and tb.mint:
                owned.setdefault(tb.owner, set()).add(tb.mint)
        for k_, d in out:
            if k_ == "amm" and d["pool"] not in pools:
                ms = owned.get(d["pool"], set())
                if len(ms) == 2 and SOL_MINT in ms:
                    pools[d["pool"]] = (next(iter(ms - {SOL_MINT})), SOL_MINT)
    if not from_ix and not meta.log_messages_none:
        # emit! пишет событие в лог «Program data: <base64>» (без тега emit_cpi)
        for line in meta.log_messages:
            if not line.startswith("Program data: "):
                continue
            try:
                raw = base64.b64decode(line[14:])
            except Exception:
                continue
            r = _decode(raw, raw[:8] in (EV_AMM_BUY, EV_AMM_SELL))
            if r == "bad":
                rejects += 1
            elif r:
                out.append(r)
    return [(k, d, keyset) for k, d in out], rejects


def _decode(ev: bytes, amm: bool):
    disc, body = ev[:8], ev[8:]
    try:
        if not amm:
            if disc == EV_TRADE:
                return ("trade", parse_trade(body))
            if disc == EV_CREATE:
                return ("create", parse_create(body))
            if disc == EV_COMPLETE:
                return ("complete", parse_complete(body))
        else:
            if disc == EV_AMM_BUY:
                return ("amm", parse_amm(body, True))
            if disc == EV_AMM_SELL:
                return ("amm", parse_amm(body, False))
    except (struct.error, ValueError, IndexError):
        return "bad"
    return None


def _b58dec(s: str) -> bytes:
    import base58
    return base58.b58decode(s)


_PUMP_B = _b58dec(PUMP_PROGRAM)
_AMM_B = _b58dec(AMM_PROGRAM)


# ── состояние по токену ──────────────────────────────────────────────────────
class MintFlow:
    __slots__ = ("mint", "name", "symbol", "creator", "created_ts", "create_slot", "first_seen",
                 "last_ts", "buckets", "recent", "users", "tracked", "dev_buy_sol", "dev_sell_sol",
                 "dev_sells", "snipers", "sniper_sol", "price_sol", "real_sol", "complete", "venue")

    def __init__(self, mint: str, now: float):
        self.mint = mint
        self.name = self.symbol = self.creator = ""
        self.created_ts = 0.0
        self.create_slot = 0
        self.first_seen = now
        self.last_ts = now
        self.buckets: collections.deque = collections.deque()   # [t0, buys, sells, buy_lam, sell_lam]
        self.recent: collections.deque = collections.deque()    # (ts, slot, is_buy, lamports, user)
        self.users: dict[str, float] = {}                       # покупатель → время последней покупки
        self.tracked: dict[str, int] = {}                       # tracked-адрес → чистые токены (≈в позиции)
        self.dev_buy_sol = self.dev_sell_sol = 0
        self.dev_sells = 0
        self.snipers: set = set()
        self.sniper_sol = 0
        self.price_sol = 0.0
        self.real_sol = 0
        self.complete = False
        self.venue = "curve"

    def add(self, now: float, slot: int, is_buy: bool, lamports: int, tokens: int, user: str):
        self.last_ts = now
        t0 = int(now // BUCKET_S * BUCKET_S)
        if not self.buckets or self.buckets[-1][0] != t0:
            self.buckets.append([t0, 0, 0, 0, 0])
        b = self.buckets[-1]
        if is_buy:
            b[1] += 1
            b[3] += lamports
            if len(self.users) < USERS_CAP or user in self.users:
                self.users[user] = now
        else:
            b[2] += 1
            b[4] += lamports
        self.recent.append((now, slot, is_buy, lamports, user))
        if self.creator and user == self.creator:
            if is_buy:
                self.dev_buy_sol += lamports
            else:
                self.dev_sell_sol += lamports
                self.dev_sells += 1
        elif is_buy and self.create_slot and slot - self.create_slot <= SNIPER_SLOTS:
            self.snipers.add(user)
            self.sniper_sol += lamports
        self.trim(now)

    def trim(self, now: float):
        while self.buckets and self.buckets[0][0] < now - KEEP_S - BUCKET_S:
            self.buckets.popleft()
        while self.recent and (self.recent[0][0] < now - RECENT_S or len(self.recent) > RECENT_CAP):
            self.recent.popleft()


class PumpFlow:
    def __init__(self):
        self._lock = threading.Lock()
        self._m: dict[str, MintFlow] = {}
        self._pools: dict[str, tuple[str, str]] = {}
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._tracked: dict = {}
        self._tracked_at = 0.0
        self._last_gc = 0.0
        self._last_ingest = 0.0
        self.stats = dict(state="off", gaps=0, tx=0, trades=0, amm_trades=0, creates=0, completes=0, rejects=0,
                          dropped=0, unmapped_pool=0, non_sol=0, verify_fail=0, lag_ms=None)
        self._lags: collections.deque = collections.deque(maxlen=500)

    # ── вход из стрима (нить gRPC: только положить и выйти) ─────────────────
    def submit(self, upd):
        if self._thread is None:
            self.start()
        try:
            self._q.put_nowait((upd, time.time()))
        except queue.Full:
            self.stats["dropped"] += 1

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self._started_at = time.time()
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, daemon=True, name="pumpflow")
            self._thread.start()

    def _loop(self):
        while True:
            upd, recv = self._q.get()
            try:
                self.ingest(upd, recv)
            except Exception as e:   # битое обновление не должно ронять нить
                self.stats["rejects"] += 1
                self.stats["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    def _tracked_set(self) -> dict:
        now = time.time()
        if now - self._tracked_at > 60:
            try:
                import wallets
                self._tracked = wallets.TRACKED
            except Exception:
                self._tracked = {}
            self._tracked_at = now
        return self._tracked

    # ── разбор одного обновления ────────────────────────────────────────────
    def ingest(self, upd, recv: float | None = None):
        now = recv or time.time()
        if self._last_ingest and now - self._last_ingest > GAP_S:
            # стрим молчал/рвался: сделки за это время потеряны → окна до сейчас неполные
            self._started_at = now
            self.stats["gaps"] += 1
        self._last_ingest = now
        if upd.HasField("created_at"):
            self._lags.append(now - (upd.created_at.seconds + upd.created_at.nanos / 1e9))
        tx = upd.transaction
        slot = int(tx.slot)
        events, rej = iter_events(tx.transaction, self._pools)
        self.stats["tx"] += 1
        self.stats["rejects"] += rej
        if not events:
            return
        tracked = self._tracked_set()
        with self._lock:
            for kind, d, keyset in events:
                if kind == "trade":
                    self._on_trade(d, keyset, slot, now, tracked)
                elif kind == "amm":
                    self._on_amm(d, slot, now, tracked)
                elif kind == "create":
                    self._on_create(d, keyset, slot, now)
                elif kind == "complete":
                    mf = self._m.get(d["mint"])
                    if mf:
                        mf.complete = True
                    self.stats["completes"] += 1
            if now - self._last_gc > 30:
                self._gc(now)

    def _get(self, mint: str, now: float) -> MintFlow:
        mf = self._m.get(mint)
        if mf is None:
            mf = self._m[mint] = MintFlow(mint, now)
        return mf

    def _on_create(self, d, keyset, slot, now):
        if _b58dec(d["mint"]) not in keyset:
            self.stats["verify_fail"] += 1
            return
        mf = self._get(d["mint"], now)
        mf.name, mf.symbol, mf.creator = d["name"], d["symbol"], d["creator"]
        mf.created_ts, mf.create_slot = float(d["ts"]), slot
        self.stats["creates"] += 1

    def _on_trade(self, d, keyset, slot, now, tracked):
        if _b58dec(d["mint"]) not in keyset:
            self.stats["verify_fail"] += 1
            return
        if d["sol"] <= 0 or d["vsol"] <= 0:
            self.stats["non_sol"] += 1              # кривая с не-SOL котировкой (vsol=0) — не наш рынок
            return
        mf = self._get(d["mint"], now)
        if not mf.creator and d.get("creator"):
            mf.creator = d["creator"]
        if d["vtok"] > 0:
            mf.price_sol = (d["vsol"] / 1e9) / (d["vtok"] / 10 ** TOKEN_DECIMALS)
        mf.real_sol = d["real_sol"]
        mf.venue = "curve"
        mf.add(now, slot, d["is_buy"], d["sol"], d["tokens"], d["user"])
        self._track(mf, d["user"], d["is_buy"], d["tokens"], tracked)
        self.stats["trades"] += 1

    def _on_amm(self, d, slot, now, tracked):
        pair = self._pools.get(d["pool"])
        if not pair:
            self.stats["unmapped_pool"] += 1
            return
        base, quote = pair
        if quote == SOL_MINT:                       # канонический пул после миграции: token/SOL
            mint, is_buy, lam, tok, r_sol, r_tok = base, d["is_buy"], d["sol"], d["tokens"], d["pool_quote"], d["pool_base"]
        elif base == SOL_MINT:                      # «перевёрнутый» пул SOL/token: buy base(SOL) = продажа токена
            mint, is_buy, lam, tok, r_sol, r_tok = quote, not d["is_buy"], d["tokens"], d["sol"], d["pool_base"], d["pool_quote"]
        else:
            return                                  # не-SOL пулы (USDC и т.п.) не наш сценарий
        if lam <= 0:
            return
        mf = self._get(mint, now)
        if r_tok > 0:
            mf.price_sol = (r_sol / 1e9) / (r_tok / 10 ** TOKEN_DECIMALS)
        mf.venue = "amm"
        mf.complete = True
        mf.add(now, slot, is_buy, lam, tok, d["user"])
        self._track(mf, d["user"], is_buy, tok, tracked)
        self.stats["amm_trades"] += 1

    @staticmethod
    def _track(mf: MintFlow, user: str, is_buy: bool, tokens: int, tracked: dict):
        if user in tracked:
            mf.tracked[user] = mf.tracked.get(user, 0) + (tokens if is_buy else -tokens)

    def _gc(self, now: float):
        self._last_gc = now
        dead = [m for m, mf in self._m.items() if now - mf.last_ts > KEEP_S]
        for m in dead:
            del self._m[m]
        if len(self._m) > MAX_MINTS:
            for m in sorted(self._m, key=lambda k: self._m[k].last_ts)[:len(self._m) - MAX_MINTS]:
                del self._m[m]
        if len(self._pools) > 50000:
            self._pools.clear()                     # перезаполнится из инструкций buy/sell

    # ── чтение ──────────────────────────────────────────────────────────────
    def _metrics(self, mf: MintFlow, window_s: float, now: float) -> dict:
        lo = now - window_s
        buys = sells = buy_l = sell_l = 0
        for t0, b, s, bl, sl in mf.buckets:
            if t0 + BUCKET_S > lo:
                buys += b; sells += s; buy_l += bl; sell_l += sl
        n = buys + sells
        vol = buy_l + sell_l
        observed_from = max(self._started_at, mf.created_ts or 0)
        tracked_in = [a for a, net in mf.tracked.items() if net > 0]
        tr = self._tracked_set()
        return dict(
            mint=mf.mint, symbol=mf.symbol, name=mf.name, venue=mf.venue,
            window_s=int(window_s),
            buys=buys, sells=sells,
            buy_ratio=round(buys / n, 4) if n else None,
            buy_vol_sol=round(buy_l / 1e9, 4), sell_vol_sol=round(sell_l / 1e9, 4),
            vol_buy_ratio=round(buy_l / vol, 4) if vol else None,
            net_flow_sol=round((buy_l - sell_l) / 1e9, 4),
            unique_buyers=sum(1 for ts in mf.users.values() if ts >= lo),
            price_sol=mf.price_sol,
            mcap_sol=round(mf.price_sol * PUMP_SUPPLY, 2),
            curve_sol=round(mf.real_sol / 1e9, 3), complete=mf.complete,
            age_s=round(now - mf.created_ts) if mf.created_ts else None,
            creator=mf.creator,
            dev_buy_sol=round(mf.dev_buy_sol / 1e9, 4), dev_sell_sol=round(mf.dev_sell_sol / 1e9, 4),
            dev_sells=mf.dev_sells,
            snipers=len(mf.snipers), sniper_sol=round(mf.sniper_sol / 1e9, 4),
            tracked_in=len(tracked_in),
            tracked_names=[(tr.get(a) or {}).get("name", a[:6]) for a in tracked_in][:10],
            last_trade_ago_s=round(now - mf.last_ts, 1),
            # окно целиком видели (стрим шёл всё окно или токен моложе окна) — иначе цифры занижены
            full=observed_from <= lo or (mf.created_ts > 0 and mf.created_ts >= self._started_at),
        )

    def metrics(self, mint: str, window_s: float = 300) -> dict | None:
        now = time.time()
        with self._lock:
            mf = self._m.get(mint)
            return self._metrics(mf, window_s, now) if mf else None

    def hot(self, window_s: float = 300, n: int = 50, min_buyers: int = 5) -> list[dict]:
        """Самые денежные токены окна (по объёму покупок) — кандидаты для скана."""
        now = time.time()
        with self._lock:
            rows = [self._metrics(mf, window_s, now) for mf in self._m.values() if now - mf.last_ts <= window_s]
        rows = [r for r in rows if r["unique_buyers"] >= min_buyers]
        rows.sort(key=lambda r: -r["buy_vol_sol"])
        return rows[:n]

    def snapshot(self) -> dict:
        s = dict(self.stats)
        lags = sorted(self._lags)
        s["lag_ms"] = round(lags[len(lags) // 2] * 1000) if lags else None
        s["mints"] = len(self._m)
        s["pools"] = len(self._pools)
        s["queue"] = self._q.qsize()
        s["uptime_s"] = round(time.time() - self._started_at) if self._started_at else 0
        return s


FLOW = PumpFlow()


# ── сверка с GMGN (шаг 1: поток идёт параллельно, решения пока на GMGN) ────────
class GmgnCompare:
    """Те же токены из trending GMGN (окно 1h) ↔ наш поток за 3600 с. Копим пары, чтобы
    до переключения скана увидеть: совпадает ли перевес/число сделок, сколько trending мы
    вообще видим. Журнал flow_compare.jsonl — на разбор вместе с исходами позиций."""

    def __init__(self, flow: PumpFlow):
        self.flow = flow
        self._lock = threading.Lock()
        self.rows: collections.deque = collections.deque(maxlen=3000)
        self._logged: dict[str, float] = {}
        self.seen = self.found = self.full = 0

    def observe(self, gmgn_rows: list, path=None, log_every_s: float = 600):
        if self.flow.stats["state"] != "on":
            return
        now = time.time()
        out = []
        for r in gmgn_rows or []:
            mint = r.get("address")
            if not mint:
                continue
            self.seen += 1
            m = self.flow.metrics(mint, KEEP_S)
            if not m:
                continue
            self.found += 1
            if not m["full"]:
                continue
            self.full += 1
            try:
                gb, gs = int(float(r.get("buys") or 0)), int(float(r.get("sells") or 0))
            except (TypeError, ValueError):
                continue
            rec = dict(ts=round(now), mint=mint, sym=str(r.get("symbol") or "")[:20],
                       g_buys=gb, g_sells=gs, g_br=round(gb / (gb + gs), 4) if gb + gs else None,
                       g_vol_usd=r.get("volume"),
                       f_buys=m["buys"], f_sells=m["sells"], f_br=m["buy_ratio"],
                       f_vbr=m["vol_buy_ratio"], f_vol_sol=round(m["buy_vol_sol"] + m["sell_vol_sol"], 3),
                       f_uniq=m["unique_buyers"], f_snipers=m["snipers"], f_dev_sell=m["dev_sell_sol"],
                       f_tracked=m["tracked_in"], venue=m["venue"])
            with self._lock:
                self.rows.append(rec)
                if path and now - self._logged.get(mint, 0) >= log_every_s:
                    self._logged[mint] = now
                    out.append(rec)
                if len(self._logged) > 20000:
                    self._logged.clear()
        if out and path:
            try:
                import json
                with open(path, "a") as fh:
                    for rec in out:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def summary(self) -> dict:
        with self._lock:
            last: dict[str, dict] = {}
            for r in self.rows:
                last[r["mint"]] = r                 # по токену — последняя пара
        pairs = [r for r in last.values() if r["g_br"] is not None and r["f_br"] is not None]
        diffs = sorted(abs(r["g_br"] - r["f_br"]) for r in pairs)
        cnt = sorted((r["f_buys"] + r["f_sells"]) / (r["g_buys"] + r["g_sells"])
                     for r in pairs if r["g_buys"] + r["g_sells"] > 0)
        agree = sum(1 for r in pairs if (r["g_br"] >= 0.65) == (r["f_br"] >= 0.65))

        def med(a):
            return round(a[len(a) // 2], 4) if a else None
        return dict(trending_seen=self.seen, in_flow=self.found, full_window=self.full,
                    coverage=round(self.found / self.seen, 3) if self.seen else None,
                    tokens=len(pairs), buy_ratio_abs_diff_med=med(diffs),
                    buy_ratio_abs_diff_p90=round(diffs[int(len(diffs) * 0.9)], 4) if diffs else None,
                    trades_flow_to_gmgn_med=med(cnt),
                    gate65_agree=round(agree / len(pairs), 3) if pairs else None)


COMPARE = GmgnCompare(FLOW)
