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
import heapq
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
LAUNCH_SLOTS = 5         # окно запуска для поиска бандлов (≈2 с)
SNIPER_SLOTS = 2        # покупка в слоте создания … +2 слота = снайпер/бандл
QUEUE_MAX = 20000
WALLETS_CAP = 400_000
PRO_MIN_CLOSED = 8        # «про»: ≥8 закрытых позиций, ≥55% в плюс, ≥ +2 SOL реализовано
PRO_MIN_WINRATE = 0.55
PRO_MIN_PNL_SOL = 2.0
MIN_TRADE_LAMPORTS = 100_000   # меньше 0.0001 SOL — пыль/комиссии, не сделка
POOL_FRESH_S = 600      # пул без сделок дольше — не кандидат в «главный» для цены
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
    if any(k == "amm" for k, d in out):
        # точность токенов из балансов tx: у не-pump токенов бывает не 6 знаков → цена ×1000 мимо
        decs = {tb.mint: tb.ui_token_amount.decimals for tb in list(meta.pre_token_balances) + list(meta.post_token_balances)
                if tb.mint and tb.HasField("ui_token_amount")}
        for k_, d in out:
            if k_ == "amm":
                d["decs"] = decs
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


_CANON: dict[str, str] = {}


def canonical_pool(mint: str) -> str:
    """Адрес пула PumpSwap, который pump.fun создаёт при миграции (index 0, creator = PDA
    «pool-authority» токена). Проверено на живых данных 16.10: главный пул = канонический у
    408 из 409 мигрировавших; исключение — торговля в чужом, вручную созданном пуле."""
    c = _CANON.get(mint)
    if c is None:
        from solders.pubkey import Pubkey
        m = Pubkey.from_string(mint)
        auth, _ = Pubkey.find_program_address([b"pool-authority", bytes(m)], Pubkey.from_string(PUMP_PROGRAM))
        pool, _ = Pubkey.find_program_address(
            [b"pool", (0).to_bytes(2, "little"), bytes(auth), bytes(m), bytes(Pubkey.from_string(SOL_MINT))],
            Pubkey.from_string(AMM_PROGRAM))
        c = _CANON[mint] = str(pool)
        if len(_CANON) > 100000:
            _CANON.clear()
    return c


# программы площадок — только для метки venue у сделок из списка наблюдения (venues.py)
VENUE_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium-cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VWaH5gzZHKZ5qy": "raydium-clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium-launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora-dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora-damm",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora-damm-v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora-dbc",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca",
}


def balance_trades(tx_info, watch: set) -> list[dict]:
    """Сделки с токенами из списка наблюдения — по изменению балансов подписанта (любая площадка,
    любой маршрут): токен ↑ и SOL(+WSOL) ↓ — покупка, наоборот — продажа. Арбитраж/круг (токен
    не изменился) и не-SOL котировки пропускаются."""
    meta = tx_info.meta
    msg = tx_info.transaction.message
    if not msg.account_keys:
        return []
    signer = b58(msg.account_keys[0])
    pre: dict[tuple, int] = {}
    post: dict[tuple, int] = {}
    dec: dict[str, int] = {}
    seen = set()
    for src, dst in ((meta.pre_token_balances, pre), (meta.post_token_balances, post)):
        for tb in src:
            if tb.owner != signer:
                continue
            try:
                amt = int(tb.ui_token_amount.amount or 0)
            except ValueError:
                continue
            dst[tb.mint] = dst.get(tb.mint, 0) + amt
            dec[tb.mint] = tb.ui_token_amount.decimals
            seen.add(tb.mint)
    mints = [m for m in seen if m in watch]
    if not mints:
        return []
    sol = 0
    if len(meta.pre_balances) and len(meta.post_balances):
        sol = int(meta.post_balances[0]) - int(meta.pre_balances[0]) + int(meta.fee)
    sol += post.get(SOL_MINT, 0) - pre.get(SOL_MINT, 0)
    # программы площадок часто в таблицах адресов (ALT) — смотрим и загруженные адреса
    keys = {b58(k) for k in list(msg.account_keys) + list(meta.loaded_writable_addresses)
            + list(meta.loaded_readonly_addresses)}
    venue = next((v for pid, v in VENUE_PROGRAMS.items() if pid in keys), "other")
    out = []
    for m in mints:
        dt = post.get(m, 0) - pre.get(m, 0)
        if dt > 0 and sol < -MIN_TRADE_LAMPORTS:
            out.append(dict(mint=m, is_buy=True, lamports=-sol, tokens=dt, dec=dec[m], user=signer, venue=venue))
        elif dt < 0 and sol > MIN_TRADE_LAMPORTS:
            out.append(dict(mint=m, is_buy=False, lamports=sol, tokens=-dt, dec=dec[m], user=signer, venue=venue))
    return out if len(out) == 1 else []          # несколько токенов за раз — маршрут/арбитраж, цену не делим


def _b58dec(s: str) -> bytes:
    import base58
    return base58.b58decode(s)


_PUMP_B = _b58dec(PUMP_PROGRAM)
_AMM_B = _b58dec(AMM_PROGRAM)


# ── состояние по токену ──────────────────────────────────────────────────────
SHOCK_RECORD = 0.08     # продажа, уронившая цену ≥8% или забравшая ≥8% SOL пула, — в shocks токена


class MintFlow:
    __slots__ = ("mint", "name", "symbol", "creator", "created_ts", "create_slot", "first_seen",
                 "last_ts", "buckets", "recent", "users", "tracked", "dev_buy_sol", "dev_sell_sol",
                 "dev_sells", "dev_tok_in", "dev_tok_out", "dev_first_sell", "dev_last_sell", "dev_rebuy_sol",
                 "dev_rebuys", "dev_rebuy_delay", "bal", "reserve_tok", "pool_px", "cost", "candles", "ticks60", "peak_px",
                 "create_wall", "launch_px", "launch_max10", "rpnl", "slot_buyers", "snipers", "sniper_sol", "price_sol", "real_sol", "complete", "venue", "shocks")

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
        # последовательность действий дева: шейкаут (продал → откупил) vs раг (продал всё и тишина)
        self.dev_tok_in = self.dev_tok_out = 0
        self.dev_first_sell = self.dev_last_sell = 0.0
        self.dev_rebuy_sol = 0
        self.dev_rebuys = 0
        self.dev_rebuy_delay: float | None = None   # с первой продажи до первого откупа, с
        self.bal: dict[str, int] = {}               # кошелёк → чистые токены по нашим наблюдениям
        self.reserve_tok = 0                        # токены в резерве кривой/пула (для ёмкости дампа)
        self.pool_px: dict[str, tuple] = {}         # пул → (цена SOL, резерв SOL, резерв токенов, ts)
        self.cost: dict[str, int] = {}              # кошелёк → потраченные лампорты на текущие токены (средняя цена)
        self.rpnl: dict[str, int] = {}              # кошелёк → реализованный PnL по этому токену, лампорты
        self.slot_buyers: dict[int, set] = {}      # слот запуска → покупатели (бандлы Jito: много кошельков в одном слоте)
        self.candles: collections.deque = collections.deque(maxlen=61)   # [t0, open, high, low, close, max_drop_60s]
        self.ticks60: collections.deque = collections.deque()             # (ts, цена) за последние 60 с
        self.peak_px = 0.0
        self.create_wall = 0.0                      # когда МЫ увидели создание (для «первых 10 секунд»)
        self.launch_px = 0.0                        # первая цена после создания
        self.launch_max10 = 0.0                     # максимум цены за первые 10 с
        self.snipers: set = set()
        self.sniper_sol = 0
        self.price_sol = 0.0
        self.real_sol = 0
        self.complete = False
        self.venue = "curve"
        self.shocks: collections.deque = collections.deque(maxlen=8)   # (ts, падение цены, доля SOL пула) — крупные продажи

    def add(self, now: float, slot: int, is_buy: bool, lamports: int, tokens: int, user: str):
        """Учёт сделки. Для продажи из позиции с известной себестоимостью → (pnl_лампорты, закрыта)."""
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
        realized = None
        if user in self.bal or len(self.bal) < USERS_CAP:
            held = self.bal.get(user, 0)
            if is_buy:
                self.cost[user] = self.cost.get(user, 0) + lamports
            elif held > 0 and tokens > held * 1.02:
                # продаёт больше, чем мы видели купленным: токены пришли мимо потока (другой пул,
                # Raydium, маршрут Jupiter, перевод) — себестоимость неизвестна, позицию не считаем
                # (16.10: арбитражные боты давали фальшивые −50 SOL)
                self.cost[user] = 0
                self.rpnl[user] = 0
            elif held > 0:                          # продажа уменьшает себестоимость пропорционально
                c0 = self.cost.get(user, 0)
                self.cost[user] = int(c0 * max(0, held - tokens) / held)
                pnl = lamports - (c0 - self.cost[user])
                self.rpnl[user] = self.rpnl.get(user, 0) + pnl
                closed = held - tokens <= held * 0.02
                realized = (pnl, closed, self.rpnl[user])
                if closed:
                    self.rpnl[user] = 0                 # следующий заход в этот токен — новая позиция
            self.bal[user] = held + (tokens if is_buy else -tokens)
        if self.creator and user == self.creator:
            if is_buy:
                self.dev_buy_sol += lamports
                self.dev_tok_in += tokens
                if self.dev_first_sell:
                    self.dev_rebuy_sol += lamports
                    self.dev_rebuys += 1
                    if self.dev_rebuy_delay is None:
                        self.dev_rebuy_delay = now - self.dev_first_sell
            else:
                self.dev_sell_sol += lamports
                self.dev_sells += 1
                self.dev_tok_out += tokens
                self.dev_first_sell = self.dev_first_sell or now
                self.dev_last_sell = now
        elif is_buy and self.create_slot and slot - self.create_slot <= SNIPER_SLOTS:
            self.snipers.add(user)
            self.sniper_sol += lamports
        if is_buy and self.create_slot and 0 <= slot - self.create_slot <= LAUNCH_SLOTS and user != self.creator:
            self.slot_buyers.setdefault(slot, set()).add(user)
        self.trim(now)
        return realized

    def tick_price(self, now: float):
        """Минутные свечи, пик, цена запуска — после каждого обновления price_sol."""
        px = self.price_sol
        if px <= 0:
            return
        self.peak_px = max(self.peak_px, px)
        t0 = int(now // 60 * 60)
        c = self.candles[-1] if self.candles else None
        if c is None or c[0] != t0:
            self.candles.append([t0, c[4] if c else px, px, px, px, 0.0])
            c = self.candles[-1]
        c[2] = max(c[2], px)
        c[3] = min(c[3], px)
        c[4] = px
        # «обвал за минуту» с учётом порядка: от максимума последних 60 с вниз к текущей цене
        self.ticks60.append((now, px))
        while self.ticks60 and self.ticks60[0][0] < now - 60:
            self.ticks60.popleft()
        if len(self.ticks60) > 400:
            self.ticks60.popleft()
        roll_hi = max(v for _, v in self.ticks60)
        c[5] = max(c[5], 1 - px / roll_hi)
        if self.create_wall and now - self.create_wall <= 10:
            self.launch_px = self.launch_px or px
            self.launch_max10 = max(self.launch_max10, px)

    def trim(self, now: float):
        while self.buckets and self.buckets[0][0] < now - KEEP_S - BUCKET_S:
            self.buckets.popleft()
        while self.recent and (self.recent[0][0] < now - RECENT_S or len(self.recent) > RECENT_CAP):
            self.recent.popleft()


class PumpFlow:
    def __init__(self):
        # монитор позиций (24.10): по токенам из watch каждая сделка будит on_watch(mint) — стоп
        # проверяется по цене потока за ~секунду, а не по DexScreener с опозданием 30–60 с
        self.watch: set[str] = set()
        self.on_watch = None
        self._lock = threading.Lock()
        self._m: dict[str, MintFlow] = {}
        self._pools: dict[str, tuple[str, str]] = {}
        self._dec: dict[str, int] = {}              # mint → decimals (из балансов tx)
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._tracked: dict = {}
        self._tracked_at = 0.0
        self._last_gc = 0.0
        self._last_ingest = 0.0
        self._creators: dict[str, list] = {}       # создатель → судьбы прошлых токенов
        self._wallets: dict[str, list] = {}        # кошелёк → сводка прибыли (про-трейдеры)
        self._watch: set[str] = set()              # токены вне pump.fun под наблюдением (venues.py)
        self.copy_events: collections.deque = collections.deque(maxlen=5000)   # (mint, кошелёк, ts)
        self._creators_saved = time.time()
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
            self.load_creators()
            self.load_wallets()
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
        if self._watch and not events:
            bt = balance_trades(tx.transaction, self._watch)
            if bt:
                tracked = self._tracked_set()
                with self._lock:
                    for d in bt:
                        self._on_balance_trade(d, slot, now, tracked)
            return
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

    def set_watch(self, mints):
        """Токены вне pump.fun (Raydium/Meteora/Orca…), которые разбираем по балансам (venues.py)."""
        self._watch = {m for m in mints if m and not m.endswith("pump")}

    def _on_balance_trade(self, d, slot, now, tracked):
        if d["tokens"] <= 0:
            return
        mf = self._get(d["mint"], now)
        px = (d["lamports"] / 1e9) / (d["tokens"] / 10 ** d["dec"])           # цена сделки (с комиссией)
        if mf.price_sol > 0 and not (0.2 <= px / mf.price_sol <= 5):
            # скачок ×5 за сделку — почти всегда кривая сделка (маршрут, рента ATA на пыли);
            # 16.10 живая проверка: один токен ×60 мимо DexScreener → цену не двигаем
            self.stats["venue_outliers"] = self.stats.get("venue_outliers", 0) + 1
        else:
            mf.price_sol = px
        mf.venue = d["venue"]
        mf.complete = True
        mf.tick_price(now)
        self._ledger(d["user"], mf.add(now, slot, d["is_buy"], d["lamports"], d["tokens"], d["user"]), d["lamports"], now)
        self._track(mf, d["user"], d["is_buy"], d["tokens"], tracked)
        self.stats["venue_trades"] = self.stats.get("venue_trades", 0) + 1

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
        mf.create_wall = now
        self.stats["creates"] += 1

    def _after_trade(self, mf: MintFlow, old_px: float, is_buy: bool, lam: int, pool_sol: int, now: float):
        """Крупная продажа одной транзакцией → в shocks; токен под наблюдением → будим монитор."""
        if not is_buy and old_px > 0 and mf.price_sol > 0:
            drop = 1 - mf.price_sol / old_px
            share = lam / (pool_sol + lam) if pool_sol > 0 else 0.0
            if drop >= SHOCK_RECORD or share >= SHOCK_RECORD:
                mf.shocks.append((now, round(drop, 4), round(share, 4)))
        if mf.mint in self.watch and self.on_watch:
            try:
                self.on_watch(mf.mint)
            except Exception:
                pass

    def _on_trade(self, d, keyset, slot, now, tracked):
        if _b58dec(d["mint"]) not in keyset:
            self.stats["verify_fail"] += 1
            return
        if d["sol"] <= 0 or d["vsol"] <= 0:
            self.stats["non_sol"] += 1              # кривая с не-SOL котировкой (vsol=0) — не наш рынок
            return
        mf = self._get(d["mint"], now)
        old_px = mf.price_sol
        if not mf.creator and d.get("creator"):
            mf.creator = d["creator"]
        if d["vtok"] > 0:
            mf.price_sol = (d["vsol"] / 1e9) / (d["vtok"] / 10 ** TOKEN_DECIMALS)
        mf.real_sol = d["real_sol"]
        mf.reserve_tok = d["vtok"]
        mf.venue = "curve"
        mf.tick_price(now)
        self._ledger(d["user"], mf.add(now, slot, d["is_buy"], d["sol"], d["tokens"], d["user"]), d["sol"], now)
        self._track(mf, d["user"], d["is_buy"], d["tokens"], tracked)
        self.stats["trades"] += 1
        self._after_trade(mf, old_px, d["is_buy"], d["sol"], d["real_sol"], now)

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
        old_px = mf.price_sol
        dec = (d.get("decs") or {}).get(mint)
        if dec is not None:
            self._dec[mint] = dec
        dec = self._dec.get(mint, TOKEN_DECIMALS if mint.endswith("pump") else None)
        if r_tok > 0 and dec is not None:
            # 13.10: у токена бывает несколько пулов (канонический + чьи-то ещё) с разной ценой —
            # цена «последней сделки» прыгала между ними и рисовала ×10⁹ и −99.8%. Цена токена =
            # цена ГЛАВНОГО пула (больше всего SOL) среди обновлённых за 10 мин.
            mf.pool_px[d["pool"]] = ((r_sol / 1e9) / (r_tok / 10 ** dec), r_sol, r_tok, now)
            live = [v for v in mf.pool_px.values() if now - v[3] <= POOL_FRESH_S]
            best = max(live, key=lambda v: v[1])
            mf.price_sol, mf.reserve_tok = best[0], best[2]
            if len(mf.pool_px) > 8:
                for k in sorted(mf.pool_px, key=lambda k: mf.pool_px[k][3])[:len(mf.pool_px) - 8]:
                    mf.pool_px.pop(k, None)
        mf.venue = "amm"
        mf.complete = True
        mf.tick_price(now)
        self._ledger(d["user"], mf.add(now, slot, is_buy, lam, tok, d["user"]), lam, now)
        self._track(mf, d["user"], is_buy, tok, tracked)
        self.stats["amm_trades"] += 1
        self._after_trade(mf, old_px, is_buy, lam, r_sol, now)

    # ── про-трейдеры: реальная прибыль каждого кошелька по нашему потоку ──
    def _ledger(self, user: str, realized, lamports: int, now: float):
        """Сводка по кошельку: [реализовано лампорт, закрыто позиций, из них в плюс, оборот, последняя]."""
        if realized is None:
            return
        pnl, closed, pos_total = realized
        led = self._wallets.get(user)
        if led is None:
            if len(self._wallets) >= WALLETS_CAP:
                return
            led = self._wallets[user] = [0, 0, 0, 0, 0.0]
        led[0] += pnl
        led[3] += lamports
        led[4] = now
        if closed:
            led[1] += 1
            led[2] += 1 if pos_total > 0 else 0

    def is_pro(self, user: str) -> bool:
        led = self._wallets.get(user)
        return bool(led and led[1] >= PRO_MIN_CLOSED and led[2] >= PRO_MIN_WINRATE * led[1]
                    and led[0] >= PRO_MIN_PNL_SOL * 1e9)

    def pro_stats(self, n: int = 20) -> list[dict]:
        rows = [(u, l) for u, l in self._wallets.items() if self.is_pro(u)]
        rows.sort(key=lambda r: -r[1][0])
        return [dict(wallet=u, pnl_sol=round(l[0] / 1e9, 2), closed=l[1], winrate=round(l[2] / l[1], 2),
                     volume_sol=round(l[3] / 1e9, 1)) for u, l in rows[:n]]

    def _wallets_path(self):
        return self._creators_path().with_name("flow_wallets.json")

    def save_wallets(self):
        try:
            import json
            keep = {u: l for u, l in self._wallets.items() if l[1] >= 2}     # одиночки не нужны на диске
            p = self._wallets_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(keep, separators=(",", ":")))
            tmp.replace(p)
        except OSError:
            pass

    def load_wallets(self):
        try:
            import json
            self._wallets = json.loads(self._wallets_path().read_text())
        except (OSError, ValueError):
            self._wallets = {}

    def _track(self, mf: MintFlow, user: str, is_buy: bool, tokens: int, tracked: dict):
        if user in tracked:
            mf.tracked[user] = mf.tracked.get(user, 0) + (tokens if is_buy else -tokens)
            if is_buy:      # копи-тест (22.10): покупка кошелька из списка → образец «зайти следом»
                self.copy_events.append((mf.mint, user, time.time()))

    def _gc(self, now: float):
        self._last_gc = now
        dead = [m for m, mf in self._m.items() if now - mf.last_ts > KEEP_S]
        for m in dead:
            self._remember_creator(self._m[m])
            del self._m[m]
        if now - self._creators_saved > 600:
            self.save_creators()
            self.save_wallets()
            if len(self._wallets) > WALLETS_CAP * 0.9:    # чистка: давно неактивные одиночки
                cut = now - 7 * 86400
                self._wallets = {u: l for u, l in self._wallets.items() if l[1] >= 2 or l[4] >= cut}
        if len(self._m) > MAX_MINTS:
            for m in sorted(self._m, key=lambda k: self._m[k].last_ts)[:len(self._m) - MAX_MINTS]:
                del self._m[m]
        if len(self._dec) > 50000:
            self._dec.clear()
        if len(self._pools) > 50000:
            self._pools.clear()                     # перезаполнится из инструкций buy/sell

    # ── чтение ──────────────────────────────────────────────────────────────
    @staticmethod
    def dump_impact(reserve_tok: int, sell_tok: int) -> float:
        """Падение цены (доля) от продажи sell_tok в кривую/пул x·y=k: цена ∝ 1/резерв², значит
        после продажи цена = (R/(R+dt))² от прежней. Точно для кривой pump.fun и пула PumpSwap."""
        if reserve_tok <= 0 or sell_tok <= 0:
            return 0.0
        return 1 - (reserve_tok / (reserve_tok + sell_tok)) ** 2

    def _dump(self, mf: MintFlow) -> dict:
        """«Ёмкость дампа»: насколько обвалится цена, если продадут дев / снайперы / топ-10
        держателей разом. Честно только при полной истории (видели создание); иначе None."""
        keys = ("dump_dev_pct", "dump_snipers_pct", "dump_top10_pct", "top10_supply_pct", "top1_supply_pct",
                "sniper_supply_pct")
        if not mf.create_slot or mf.reserve_tok <= 0:
            return dict.fromkeys(keys)
        supply = PUMP_SUPPLY * 10 ** TOKEN_DECIMALS
        dev = max(0, mf.dev_tok_in - mf.dev_tok_out)
        snip = sum(max(0, mf.bal.get(u, 0)) for u in mf.snipers)
        top = heapq.nlargest(10, (v for v in mf.bal.values() if v > 0))
        top10 = sum(top)
        R = mf.reserve_tok
        return dict(dump_dev_pct=round(100 * self.dump_impact(R, dev), 1),
                    dump_snipers_pct=round(100 * self.dump_impact(R, snip), 1),
                    dump_top10_pct=round(100 * self.dump_impact(R, top10), 1),
                    top10_supply_pct=round(100 * top10 / supply, 2),
                    top1_supply_pct=round(100 * (top[0] if top else 0) / supply, 2),
                    sniper_supply_pct=round(100 * snip / supply, 2))

    # ── история деплоеров (своя база: создатель → судьбы его прошлых токенов) ──
    def _remember_creator(self, mf: MintFlow):
        """Токен ушёл из памяти (час без сделок) — запомнить его судьбу за создателем."""
        if not mf.creator or not mf.create_slot:
            return
        peak_mc = mf.peak_px * PUMP_SUPPLY
        last_mc = mf.price_sol * PUMP_SUPPLY
        rec = dict(m=mf.mint[-8:], ts=round(mf.created_ts), peak=round(peak_mc, 1), last=round(last_mc, 1),
                   done=bool(mf.complete), dev_out=bool(mf.dev_tok_in and mf.dev_tok_out >= mf.dev_tok_in * 0.95),
                   life=round(mf.last_ts - (mf.create_wall or mf.first_seen)))
        h = self._creators.setdefault(mf.creator, [])
        h.append(rec)
        del h[:-20]
        if len(self._creators) > 150000:
            for k in list(self._creators)[:30000]:
                self._creators.pop(k, None)

    def creator_history(self, creator: str) -> dict:
        h = self._creators.get(creator) or []
        if not h:
            return dict(dev_prev_tokens=0, dev_prev_migrated=0, dev_prev_rugs=0, dev_prev_best_mcap_sol=None,
                        dev_migration_rate=None)
        # «раг» = дев вышел полностью и капа упала >80% от пика
        rugs = sum(1 for r in h if r["dev_out"] and r["peak"] > 0 and r["last"] < 0.2 * r["peak"])
        mig = sum(1 for r in h if r["done"])
        return dict(dev_prev_tokens=len(h), dev_prev_migrated=mig, dev_prev_rugs=rugs,
                    dev_prev_best_mcap_sol=max(r["peak"] for r in h),
                    dev_migration_rate=round(mig / len(h), 3))

    def _creators_path(self):
        import pathlib
        d = os.getenv("ABC_DATA_DIR", "").strip()
        base = pathlib.Path(d) if d else pathlib.Path(__file__).resolve().parent / "outputs"
        return base / "flow_creators.json"

    def save_creators(self):
        self._creators_saved = time.time()
        try:
            import json
            p = self._creators_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._creators, separators=(",", ":")))
            tmp.replace(p)
        except OSError:
            pass

    def load_creators(self):
        try:
            import json
            self._creators = json.loads(self._creators_path().read_text())
        except (OSError, ValueError):
            self._creators = {}

    def _shape(self, mf: MintFlow) -> dict:
        """Форма цены и мотивация держателей: обвалы за час, откат от пика, всплеск запуска,
        по какой цене вошли топ-25 (чем дешевле — тем сильнее соблазн слить)."""
        out = dict(crash_1m_max_pct=None, dd_from_high_pct=None, launch_x10s=None,
                   top25_basis_pct=None, top25_profit_x=None, holders_obs=None,
                   wash_share_5m=None, top3_trader_share_5m=None, lp_mc_ratio=None, turnover_1h=None)
        now = time.time()
        # накрутка (как «organic volume» у Mobula): объём кошельков, что за 5 мин и купили, и
        # продали (круги туда-обратно), и доля сделок трёх самых активных кошельков — фермы
        rec = [r for r in mf.recent if r[0] >= now - 300]
        if rec:
            sides: dict[str, set] = {}
            vol_by: dict[str, int] = {}
            cnt_by: dict[str, int] = {}
            for _, _, is_buy, lam, user in rec:
                sides.setdefault(user, set()).add(is_buy)
                vol_by[user] = vol_by.get(user, 0) + lam
                cnt_by[user] = cnt_by.get(user, 0) + 1
            total = sum(vol_by.values())
            if total > 0:
                out["wash_share_5m"] = round(sum(v for u, v in vol_by.items() if len(sides[u]) == 2) / total, 3)
            out["top3_trader_share_5m"] = round(sum(heapq.nlargest(3, cnt_by.values())) / len(rec), 3)
        if mf.price_sol > 0:
            mcap = mf.price_sol * PUMP_SUPPLY
            liq = self._liq_sol(mf, now)
            if liq > 0:
                out["lp_mc_ratio"] = round(2 * liq / mcap, 4)        # стоимость пула (две стороны) / капа
            vol1h = sum(b[3] + b[4] for b in mf.buckets if b[0] + BUCKET_S > now - 3600) / 1e9
            out["turnover_1h"] = round(vol1h / mcap, 3)
        if mf.create_slot:
            out["holders_obs"] = sum(1 for v in mf.bal.values() if v > 0)
        pros = [u for u, v in mf.bal.items() if v > 0 and self.is_pro(u)]
        out["pro_in"] = len(pros)
        out["pro_buys_5m"] = sum(1 for r in mf.recent if r[0] >= now - 300 and r[2] and self.is_pro(r[4]))
        out["launch_bundle_max"] = out["launch_bundle_wallets"] = out["launch_bundle_pct"] = None
        if mf.create_slot:
            groups = [b for b in mf.slot_buyers.values() if len(b) >= 2]
            wallets = set().union(*groups) if groups else set()
            supply = PUMP_SUPPLY * 10 ** TOKEN_DECIMALS
            out["launch_bundle_max"] = max((len(b) for b in mf.slot_buyers.values()), default=0)
            out["launch_bundle_wallets"] = len(wallets)
            out["launch_bundle_pct"] = round(100 * sum(max(0, mf.bal.get(w, 0)) for w in wallets) / supply, 2)
        out["pool_canonical"] = out["pool_count"] = None
        if mf.venue == "amm" and mf.pool_px and mf.mint.endswith("pump"):
            live = {k: v for k, v in mf.pool_px.items() if now - v[3] <= POOL_FRESH_S} or mf.pool_px
            main = max(live.items(), key=lambda kv: kv[1][1])[0]
            try:
                out["pool_canonical"] = main == canonical_pool(mf.mint)   # False = торгуют в чужом пуле
            except Exception:
                pass
            out["pool_count"] = len(live)
        cs = list(mf.candles)
        if cs:
            out["crash_1m_max_pct"] = round(100 * max(c[5] for c in cs), 1)
            hi = max(c[2] for c in cs)
            if hi > 0 and mf.price_sol > 0:
                out["dd_from_high_pct"] = round(100 * (1 - mf.price_sol / hi), 1)
        if mf.launch_px > 0:
            out["launch_x10s"] = round(mf.launch_max10 / mf.launch_px, 2)
        if mf.create_slot and mf.price_sol > 0:
            top = heapq.nlargest(25, ((v, u) for u, v in mf.bal.items() if v > 0))
            tok = sum(v for v, _ in top)
            lam = sum(mf.cost.get(u, 0) for _, u in top)
            if tok > 0 and lam > 0:
                basis = (lam / 1e9) / (tok / 10 ** TOKEN_DECIMALS)   # средняя цена входа, SOL/токен
                out["top25_basis_pct"] = round(100 * basis / mf.price_sol, 1)
                out["top25_profit_x"] = round(mf.price_sol / basis, 2)
        return out

    def _metrics(self, mf: MintFlow, window_s: float, now: float, deep: bool = True) -> dict:
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
            mcap_sol=round(mf.price_sol * PUMP_SUPPLY, 2) if mf.venue in ("curve", "amm") else None,
            liq_sol=self._liq_sol(mf, now),
            curve_sol=round(mf.real_sol / 1e9, 3), complete=mf.complete,
            age_s=round(now - mf.created_ts) if mf.created_ts else None,
            creator=mf.creator,
            dev_buy_sol=round(mf.dev_buy_sol / 1e9, 4), dev_sell_sol=round(mf.dev_sell_sol / 1e9, 4),
            dev_sells=mf.dev_sells,
            dev_rebuy_sol=round(mf.dev_rebuy_sol / 1e9, 4), dev_rebuys=mf.dev_rebuys,
            dev_rebuy_delay_s=round(mf.dev_rebuy_delay) if mf.dev_rebuy_delay is not None else None,
            # доля купленного девом, что ещё у него (по токенам; None = дев не покупал в нашем окне)
            dev_hold_share=round(max(0, mf.dev_tok_in - mf.dev_tok_out) / mf.dev_tok_in, 3) if mf.dev_tok_in else None,
            dev_last_sell_ago_s=round(now - mf.dev_last_sell) if mf.dev_last_sell else None,
            # % саплая у дева: честно только если видели создание (первая покупка дева — в tx создания)
            dev_supply_pct=round(100 * max(0, mf.dev_tok_in - mf.dev_tok_out) / (PUMP_SUPPLY * 10 ** TOKEN_DECIMALS), 2)
            if mf.create_slot else None,
            dev_supply_peak_pct=round(100 * mf.dev_tok_in / (PUMP_SUPPLY * 10 ** TOKEN_DECIMALS), 2) if mf.create_slot else None,
            snipers=len(mf.snipers), sniper_sol=round(mf.sniper_sol / 1e9, 4),
            tracked_in=len(tracked_in),
            tracked_names=[(tr.get(a) or {}).get("name", a[:6]) for a in tracked_in][:10],
            last_trade_ago_s=round(now - mf.last_ts, 1),
            # окно целиком видели (стрим шёл всё окно или токен моложе окна) — иначе цифры занижены
            full=observed_from <= lo or (mf.created_ts > 0 and mf.created_ts >= self._started_at),
            **(self._dump(mf) if deep else {}),
            **(self._shape(mf) if deep else {}),
            **(self.creator_history(mf.creator) if deep and mf.creator else {}),
        )

    def price(self, mint: str) -> tuple[float, float] | None:
        """(цена в SOL, время последней сделки) — дёшево, для разметки исходов."""
        mf = self._m.get(mint)
        return (mf.price_sol, mf.last_ts) if mf and mf.price_sol > 0 else None

    def shock_since(self, mint: str, since_ts: float) -> tuple | None:
        """Самая сильная крупная продажа после since_ts: (ts, падение цены, доля SOL пула) или None."""
        with self._lock:
            mf = self._m.get(mint)
            rows = [r for r in mf.shocks if r[0] >= since_ts] if mf else []
        return max(rows, key=lambda r: (r[1], r[2])) if rows else None

    def dead_stats(self, mint: str, window_s: float = 1800) -> dict | None:
        """«Мёртвый» токен: оборот за окно / капа и размах цены по минутным свечам. None —
        окно видели не целиком (рестарт потока) или нет цены."""
        now = time.time()
        with self._lock:
            mf = self._m.get(mint)
            if not mf or mf.price_sol <= 0:
                return None
            if self._started_at and now - self._started_at < window_s:
                return None
            lo = now - window_s
            vol = sum(b[3] + b[4] for b in mf.buckets if b[0] + BUCKET_S > lo) / 1e9
            cs = [c for c in mf.candles if c[0] >= lo]
            mcap = mf.price_sol * PUMP_SUPPLY
        if not cs:                                   # ни одной сделки за окно — точно мёртвый
            return dict(turnover=0.0, range=0.0, trades_window=0)
        hi, low = max(c[2] for c in cs), min(c[3] for c in cs)
        return dict(turnover=round(vol / mcap, 5) if mcap > 0 else None,
                    range=round(hi / low - 1, 4) if low > 0 else None, trades_window=len(cs))

    def top_holders(self, mint: str, n: int = 15) -> tuple[list, str]:
        """[(кошелёк, % саплая)] крупнейших держателей по нашим балансам + создатель.
        Только при полной истории (видели создание) — иначе балансы неполные → []."""
        with self._lock:
            mf = self._m.get(mint)
            if not mf or not mf.create_slot:
                return [], (mf.creator if mf else "")
            supply = PUMP_SUPPLY * 10 ** TOKEN_DECIMALS
            top = heapq.nlargest(n, ((v, u) for u, v in mf.bal.items() if v > 0))
            return [(u, round(100 * v / supply, 3)) for v, u in top], mf.creator

    def entry_snapshot(self, mint: str) -> dict | None:
        """Компактный ончейн-срез токена (30с/1м/5м + дев/снайперы) — признаки для входа и обучения."""
        w = {k: self.metrics(mint, sec) for k, sec in (("30s", 30), ("1m", 60), ("5m", 300))}
        m5 = w.get("5m")
        if not m5:
            return None
        snap = {k: m5.get(k) for k in ("venue", "age_s", "curve_sol", "full", "snipers", "sniper_sol", "dev_buy_sol",
                                   "dev_sell_sol", "dev_sells", "dev_rebuy_sol", "dev_rebuys", "dev_rebuy_delay_s",
                                   "dev_hold_share", "dev_last_sell_ago_s", "dev_supply_pct", "dev_supply_peak_pct",
                                   "tracked_in", "last_trade_ago_s", "price_sol", "mcap_sol", "liq_sol", "dump_dev_pct",
                                   "dump_snipers_pct", "dump_top10_pct", "top10_supply_pct", "top1_supply_pct",
                                   "sniper_supply_pct", "crash_1m_max_pct", "dd_from_high_pct", "launch_x10s", "top25_basis_pct",
                                   "top25_profit_x", "dev_prev_tokens", "dev_prev_migrated", "dev_prev_rugs",
                                   "dev_prev_best_mcap_sol", "dev_migration_rate", "holders_obs", "wash_share_5m",
                                   "top3_trader_share_5m", "lp_mc_ratio", "turnover_1h", "pool_canonical", "pool_count",
                                   "pro_in", "pro_buys_5m", "launch_bundle_max", "launch_bundle_wallets",
                                   "launch_bundle_pct")}
        for k, m in w.items():
            if m:
                snap[k] = dict(b=m["buys"], s=m["sells"], br=m["buy_ratio"], vbr=m["vol_buy_ratio"],
                               net=m["net_flow_sol"], vol=round(m["buy_vol_sol"] + m["sell_vol_sol"], 3),
                               uniq=m["unique_buyers"])
        return snap

    def metrics(self, mint: str, window_s: float = 300) -> dict | None:
        now = time.time()
        with self._lock:
            mf = self._m.get(mint)
            return self._metrics(mf, window_s, now) if mf else None

    @staticmethod
    def _liq_sol(mf: MintFlow, now: float) -> float:
        """SOL в главном пуле (PumpSwap) или реальный SOL кривой — глубина рынка под размер входа."""
        if mf.venue == "amm":
            live = [v for v in mf.pool_px.values() if now - v[3] <= POOL_FRESH_S]
            return round(max(v[1] for v in live) / 1e9, 2) if live else 0.0
        return round(mf.real_sol / 1e9, 2)

    def mid_candidates(self, min_mcap_sol: float, min_liq_sol: float, active_s: float = 300) -> list[tuple]:
        """Рынок «второго этапа» (как HOTBOT): pump-токены после миграции на PumpSwap с капой от
        порога и живыми сделками. → [(mint, mcap_sol, liq_sol)]. Без сети — только память потока."""
        now = time.time()
        out = []
        with self._lock:
            for mf in self._m.values():
                if mf.venue != "amm" or not mf.mint.endswith("pump") or mf.price_sol <= 0:
                    continue
                if now - mf.last_ts > active_s:
                    continue
                mcap = mf.price_sol * PUMP_SUPPLY
                liq = self._liq_sol(mf, now)
                if mcap >= min_mcap_sol and liq >= min_liq_sol:
                    out.append((mf.mint, round(mcap, 1), liq))
        out.sort(key=lambda r: r[1])             # с меньших кап: целевой диапазон $200k+ первым,
        return out                               # гиганты на миллиарды не съедают лимит выборки

    def hot(self, window_s: float = 300, n: int = 50, min_buyers: int = 5) -> list[dict]:
        """Самые денежные токены окна (по объёму покупок) — кандидаты для скана."""
        now = time.time()
        with self._lock:
            rows = [self._metrics(mf, window_s, now, deep=False) for mf in self._m.values() if now - mf.last_ts <= window_s]
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
        s["wallets"] = len(self._wallets)
        s["pros"] = sum(1 for u in list(self._wallets) if self.is_pro(u))
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

    def observe(self, gmgn_rows: list, path=None, log_every_s: float = 600, window_s: float = KEEP_S):
        if self.flow.stats["state"] != "on":
            return
        now = time.time()
        out = []
        for r in gmgn_rows or []:
            mint = r.get("address")
            if not mint:
                continue
            self.seen += 1
            m = self.flow.metrics(mint, min(window_s, KEEP_S))
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
            rec = dict(ts=round(now), win=int(window_s), mint=mint, sym=str(r.get("symbol") or "")[:20],
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
