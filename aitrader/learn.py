"""Самообучение, шаг 1: датасет «признаки → исход» по ВСЕМ кандидатам, а не только по сделкам.

Зачем (12.10): бот покупает ~60 раз в день — на этом модель училась бы месяцами. Но свой
поток (pumpflow) видит цену каждого pump-токена в каждой сделке, значит исход можно узнать
для любого кандидата скана — и купленного, и отклонённого. Тысячи примеров в день без
денег; плюс видно, какие хорошие токены фильтры зря отсекли.

Как: каждый кандидат house-скана (раз в DEDUP_S на токен) и топ потока по объёму → образец
с признаками (поля GMGN, решение гейтов, ончейн-срез потока) и ценой входа из потока. Дальше
нить разметки каждые TICK_S смотрит цену и копит: максимум/минимум, время первого касания
уровней (+20%…+400%, −20%…−80% — из этого симулируются любые TP/SL), цены на 2/5/15/60 мин.
Через HORIZON_S образец уходит в OUT/learn/samples-YYYY-MM-DD.jsonl. Цена всегда из одного
источника (поток, SOL) — без смешения, которое рисовало фальшивые иксы.
Env: ABC_LEARN=0 выключить.
"""
from __future__ import annotations

import json
import os
import random
import pathlib
import threading
import time

UP = (0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 4.0)
DN = (-0.1, -0.15, -0.2, -0.35, -0.5, -0.8)
CHECKPOINTS = (120, 300, 900, 1800, 3600, 7200, 14400)
HORIZON_S = 3600

# Рынок «второго этапа» (15.10, юзер: «делаем от 200к капы»; так торгует HOTBOT): pump-токены
# после миграции на PumpSwap, капа ≥ $200k. Движения медленнее → горизонт 4 ч. Каждый кандидат
# сверяется с DexScreener (капа/ликвидность/возраст/объём): цена потока и Dex расходятся >30% →
# образец suspect. Ликвидность < $15k — фермы накрутки (у них ~$2k), не берём.
MID_MIN_MCAP_USD = float(os.getenv("ABC_LEARN_MID_MCAP", "200000") or 200000)
MID_MIN_LIQ_USD = 15000.0
MID_HORIZON_S = 14400
MID_EVERY_S = 60
MID_MAX_PER_SCAN = 60
MID_PRICE_TOL = 0.30
MID_MAX_AGE_H = 168.0     # старше недели — уже не «второй этап», не берём
TICK_S = 3.0
DEDUP_S = 600           # один образец на токен за 10 мин (скан каждые 20 с)
FLOW_TOP_EVERY_S = 60   # топ потока — кандидаты помимо GMGN (охват шире trending)
FLOW_TOP_N = 30
MAX_PENDING = 20000
FUND_SAMPLE = 0.2       # доля образцов датасета, по держателям которых спрашиваем источник денег
SANE_MAX_X = 50.0       # ×50 за час на пуле — почти всегда смена пула/масштаба, помечаем suspect


def enabled() -> bool:
    return os.getenv("ABC_LEARN", "1").strip().lower() not in ("0", "false", "off", "no")


def _short_reason(r: str) -> str:
    return (r or "")[:80]


class Recorder:
    def __init__(self, out_dir: pathlib.Path | None = None, flow=None, dex_fn=None, sol_usd=None):
        self.out_dir = pathlib.Path(out_dir) if out_dir else None
        self._flow = flow
        self._dex_fn = dex_fn                     # mints → {mint: info} (тесты подменяют сеть)
        self._sol_usd = sol_usd
        self._last_mid = 0.0
        self._mid_skip: dict[str, float] = {}     # mint → до какого времени не предлагать (отказ Dex)
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}      # id -> образец в разметке
        self.last_seen: dict[str, float] = {}   # mint -> ts последнего образца
        self.stats = dict(state="off", recorded=0, labeled=0, skipped_no_flow=0, dropped=0, last_error=None,
                          mid_recorded=0, mid_candidates=0, mid_rejected_dex=0, mid_price_mismatch=0, dex_errors=0)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_flow_top = 0.0

    @property
    def flow(self):
        if self._flow is None:
            import pumpflow
            self._flow = pumpflow.FLOW
        return self._flow

    def _dir(self) -> pathlib.Path:
        if self.out_dir is None:
            d = os.getenv("ABC_DATA_DIR", "").strip()
            self.out_dir = pathlib.Path(d) if d else pathlib.Path(__file__).resolve().parent / "outputs"
        return self.out_dir / "learn"

    # ── запись образцов ──
    def _add(self, mint: str, src: str, decision: dict | None, gmgn: dict | None, now: float | None = None,
             extra: dict | None = None) -> bool:
        now = now or time.time()
        if now - self.last_seen.get(mint, 0) < DEDUP_S:
            return False
        px = self.flow.price(mint)
        snap = self.flow.entry_snapshot(mint) if px else None
        if not px or not snap:
            self.stats["skipped_no_flow"] += 1
            return False
        if len(self.pending) >= MAX_PENDING:
            self.stats["dropped"] += 1
            return False
        hold, creator = self.flow.top_holders(mint) if hasattr(self.flow, "top_holders") else ([], "")
        if hold:
            extra = dict(extra or {}, h=hold, creator=creator)
            # источники денег держателей (инсайдеры/бандлеры): кандидаты бота — сразу, датасет —
            # каждый 5-й образец (бюджет запросов Helius), кэш навсегда
            act = (decision or {}).get("action") == "ACTION"
            if act or random.random() < FUND_SAMPLE:
                try:
                    import funding
                    funding.FUND.request([w for w, _ in hold] + ([creator] if creator else []), 1 if act else 5)
                except Exception:
                    pass
        sid = f"{mint}:{int(now)}"
        smp = dict(id=sid, mint=mint, src=src, t0=now, p0=px[0], decision=decision, gmgn=gmgn, flow=snap,
                   hi=0.0, lo=0.0, up={}, dn={}, at={}, last_px=px[0], last_trade=px[1], **(extra or {}))
        with self.lock:
            self.pending[sid] = smp
            self.last_seen[mint] = now
        self.stats["recorded"] += 1
        self.start()
        return True

    def observe_decisions(self, decisions: list[dict]):
        """Решения house-скана: ACTION (прошёл все гейты) и SKIP с причиной/номером гейта."""
        if not enabled():
            return
        for d in decisions or []:
            dec = d.get("decision") or {}
            mint = dec.get("address")
            if not mint:
                continue
            self._add(mint, "gmgn", dict(action=dec.get("action"), gate=dec.get("gate"),
                                         reason=_short_reason(dec.get("reason")), priority=dec.get("priority")),
                      dec.get("features"))
        self._flow_top()

    def _flow_top(self, now: float | None = None):
        now = now or time.time()
        if now - self._last_flow_top < FLOW_TOP_EVERY_S:
            return
        self._last_flow_top = now
        try:
            for r in self.flow.hot(300, FLOW_TOP_N * 3):
                if r["mint"].endswith("pump"):       # наша вселенная — токены pump.fun (6 знаков, 1B саплай)
                    self._add(r["mint"], "flow", None, None, now)
        except Exception as e:
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    # ── рынок второго этапа (капа ≥ $200k) ──
    def sol_usd(self) -> float:
        if self._sol_usd:
            return self._sol_usd()
        import pumpcurve
        return pumpcurve.sol_usd()

    def dex(self, mints: list[str]) -> dict:
        if self._dex_fn:
            return self._dex_fn(mints)
        return dex_info(mints)

    def mid_scan(self, now: float | None = None) -> int:
        now = now or time.time()
        if now - self._last_mid < MID_EVERY_S:
            return 0
        self._last_mid = now
        rate = self.sol_usd()
        if rate <= 0:
            return 0
        cands = self.flow.mid_candidates(MID_MIN_MCAP_USD / rate, (MID_MIN_LIQ_USD / 2) / rate)
        try:                                       # + Raydium/Meteora/Orca из venues с живыми сделками
            import venues
            have = {c[0] for c in cands}
            for m in venues.WATCH.mints():
                px = self.flow.price(m)
                if m not in have and px and now - px[1] <= 300:
                    cands.append((m, 0.0, 0.0))
        except Exception:
            pass
        cands = [c for c in cands if now - self.last_seen.get(c[0], 0) >= DEDUP_S
                 and self._mid_skip.get(c[0], 0) <= now][:MID_MAX_PER_SCAN]
        self.stats["mid_candidates"] += len(cands)
        if not cands:
            return 0
        try:
            info = self.dex([c[0] for c in cands])
        except Exception as e:
            self.stats["dex_errors"] += 1
            self.stats["last_error"] = f"dex {type(e).__name__}: {e}"[:160]
            return 0
        if len(self._mid_skip) > 20000:
            self._mid_skip = {m: t for m, t in self._mid_skip.items() if t > now}
        n = 0
        for mint, mcap_sol, liq_sol in cands:
            d = info.get(mint)
            why = ("no_dex" if not d else "mcap" if d["mcap"] < MID_MIN_MCAP_USD else "liq" if d["liq"] < MID_MIN_LIQ_USD
                   else "age" if d.get("age_h") is not None and d["age_h"] > MID_MAX_AGE_H else None)
            if why:
                self.stats["mid_rejected_dex"] += 1
                self.stats["mid_rej_" + why] = self.stats.get("mid_rej_" + why, 0) + 1
                # отказ запоминаем: иначе те же старые токены каждую минуту занимают лимит выборки
                self._mid_skip[mint] = now + (6 * 3600 if why == "age" else 1800)
                continue
            px = self.flow.price(mint)
            ours_usd = px[0] * rate if px else 0.0
            ratio = ours_usd / d["price"] if d["price"] > 0 else 0.0
            extra = dict(horizon=MID_HORIZON_S, dex=d, px_ratio=round(ratio, 3))
            if not (1 - MID_PRICE_TOL <= ratio <= 1 / (1 - MID_PRICE_TOL)):
                extra["suspect"] = True                 # наш поток и Dex не сходятся — не учим на этом
                self.stats["mid_price_mismatch"] += 1
            if self._add(mint, "mid", None, None, now, extra):
                n += 1
                self.stats["mid_recorded"] += 1
        return n

    # ── копи-тест: зайти следом за кошельком из списка (22.10) ──
    def copy_scan(self) -> int:
        """Покупки кошельков из wallets.json, увиденные потоком → образец src="copy" (вход через
        ~3 с после их сделки, разметка как у всех). Проверяем: стоит ли копировать и кого."""
        ev = getattr(self.flow, "copy_events", None)
        if not ev:
            return 0
        try:
            import wallets
            names = wallets.TRACKED
        except Exception:
            names = {}
        n = 0
        while ev:
            mint, user, ts = ev.popleft()
            if time.time() - ts > 30:
                continue                              # устарело — честного «следом» уже нет
            name = (names.get(user) or {}).get("name", user[:6])
            if self._add(mint, "copy", dict(wallet=user, name=name), None, extra=dict(wallet=user, wname=name)):
                n += 1
                self.stats["copy_recorded"] = self.stats.get("copy_recorded", 0) + 1
        return n

    # ── разметка ──
    def label_tick(self, now: float | None = None) -> int:
        now = now or time.time()
        done = []
        with self.lock:
            items = list(self.pending.values())
        for smp in items:
            px = self.flow.price(smp["mint"])
            if px:
                if smp["p0"] and not (1 / 1000 <= px[0] / smp["p0"] <= SANE_MAX_X):
                    smp["suspect"] = True                # скачок цены вне физики — артефакт, а не исход
                smp["last_px"], smp["last_trade"] = px
            ret = smp["last_px"] / smp["p0"] - 1 if smp["p0"] else 0.0
            age = now - smp["t0"]
            smp["hi"] = max(smp["hi"], ret)
            smp["lo"] = min(smp["lo"], ret)
            for lv in UP:
                if ret >= lv and str(lv) not in smp["up"]:
                    smp["up"][str(lv)] = round(age)
            for lv in DN:
                if ret <= lv and str(lv) not in smp["dn"]:
                    smp["dn"][str(lv)] = round(age)
            hz = smp.get("horizon", HORIZON_S)
            for cp in CHECKPOINTS:
                if cp <= hz and age >= cp and str(cp) not in smp["at"]:
                    smp["at"][str(cp)] = round(ret, 4)
            if age >= hz:
                done.append(smp)
        if done:
            self._write(done)
            with self.lock:
                for smp in done:
                    self.pending.pop(smp["id"], None)
                if len(self.last_seen) > 50000:
                    cut = now - DEDUP_S
                    self.last_seen = {m: t for m, t in self.last_seen.items() if t >= cut}
            self.stats["labeled"] += len(done)
        return len(done)

    def _write(self, samples: list[dict]):
        d = self._dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            with open(d / f"samples-{day}.jsonl", "a") as fh:
                for smp in samples:
                    out = {k: v for k, v in smp.items() if k not in ("last_px",)}
                    if smp.get("h"):
                        try:
                            import funding
                            out["fund"] = funding.FUND.features(smp["h"], smp.get("creator"))
                        except Exception:
                            pass
                    out["hi"], out["lo"] = round(out["hi"], 4), round(out["lo"], 4)
                    out["dead_s"] = round(smp["t0"] + smp.get("horizon", HORIZON_S) - smp["last_trade"]) if smp["last_trade"] else None
                    fh.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    # ── переживать рестарт (деплой): неразмеченные образцы на диск, после старта — дальше ──
    def save_pending(self):
        try:
            d = self._dir()
            d.mkdir(parents=True, exist_ok=True)
            with self.lock:
                data = json.dumps(dict(saved=time.time(), pending=list(self.pending.values())), separators=(",", ":"))
            tmp = d / "pending.tmp"
            tmp.write_text(data)
            tmp.replace(d / "pending.json")
        except OSError as e:
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    def load_pending(self) -> int:
        """После рестарта: вернуть образцы в разметку. Пока сервер лежал, путь цены не видели —
        такие образцы помечаются gap (касания уровней за паузу могли быть пропущены)."""
        f = self._dir() / "pending.json"
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            return 0
        now = time.time()
        n = 0
        with self.lock:
            for smp in d.get("pending", []):
                if now - smp.get("t0", 0) >= smp.get("horizon", HORIZON_S) * 2 or smp["id"] in self.pending:
                    continue
                smp["gap"] = round(now - float(d.get("saved") or now))
                self.pending[smp["id"]] = smp
                self.last_seen[smp["mint"]] = max(self.last_seen.get(smp["mint"], 0), smp["t0"])
                n += 1
        return n

    def _loop(self):
        k = 0
        while not self._stop.wait(TICK_S):
            try:
                self.mid_scan()
                self.copy_scan()
                self.label_tick()
                k += 1
                if k % 20 == 0:          # ~раз в минуту
                    self.save_pending()
            except Exception as e:      # разметка никогда не роняет сервер
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    def start(self):
        if self._thread is None and enabled():
            self.stats["restored"] = self.load_pending()
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, name="learn", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        return dict(self.stats, pending=len(self.pending))


def dex_info(mints: list[str]) -> dict:
    """DexScreener пачками по 30: по каждому mint — главная пара Solana (макс. ликвидность)."""
    import httpx
    out: dict[str, dict] = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        r = httpx.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=10.0)
        r.raise_for_status()
        for p in r.json().get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            m = (p.get("baseToken") or {}).get("address")
            if m not in chunk:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m in out and out[m]["liq"] >= liq:
                continue
            vol, tx, ch = p.get("volume") or {}, p.get("txns") or {}, p.get("priceChange") or {}
            out[m] = dict(dex=p.get("dexId"), sym=str((p.get("baseToken") or {}).get("symbol") or "")[:24],
                          price=float(p.get("priceUsd") or 0), liq=liq,
                          mcap=float(p.get("marketCap") or p.get("fdv") or 0),
                          vol_h1=float(vol.get("h1") or 0), vol_h24=float(vol.get("h24") or 0),
                          buys_h1=int((tx.get("h1") or {}).get("buys") or 0), sells_h1=int((tx.get("h1") or {}).get("sells") or 0),
                          chg_h1=ch.get("h1"), chg_h6=ch.get("h6"), chg_h24=ch.get("h24"),
                          age_h=round((time.time() * 1000 - float(p.get("pairCreatedAt") or 0)) / 3.6e6, 2)
                          if p.get("pairCreatedAt") else None)
    return out


REC = Recorder()
