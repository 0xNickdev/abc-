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
import pathlib
import threading
import time

UP = (0.2, 0.5, 1.0, 2.0, 4.0)
DN = (-0.2, -0.35, -0.5, -0.8)
CHECKPOINTS = (120, 300, 900, 3600)
HORIZON_S = 3600
TICK_S = 3.0
DEDUP_S = 600           # один образец на токен за 10 мин (скан каждые 20 с)
FLOW_TOP_EVERY_S = 60   # топ потока — кандидаты помимо GMGN (охват шире trending)
FLOW_TOP_N = 30
MAX_PENDING = 20000


def enabled() -> bool:
    return os.getenv("ABC_LEARN", "1").strip().lower() not in ("0", "false", "off", "no")


def _short_reason(r: str) -> str:
    return (r or "")[:80]


class Recorder:
    def __init__(self, out_dir: pathlib.Path | None = None, flow=None):
        self.out_dir = pathlib.Path(out_dir) if out_dir else None
        self._flow = flow
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}      # id -> образец в разметке
        self.last_seen: dict[str, float] = {}   # mint -> ts последнего образца
        self.stats = dict(state="off", recorded=0, labeled=0, skipped_no_flow=0, dropped=0, last_error=None)
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
    def _add(self, mint: str, src: str, decision: dict | None, gmgn: dict | None, now: float | None = None) -> bool:
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
        sid = f"{mint}:{int(now)}"
        smp = dict(id=sid, mint=mint, src=src, t0=now, p0=px[0], decision=decision, gmgn=gmgn, flow=snap,
                   hi=0.0, lo=0.0, up={}, dn={}, at={}, last_px=px[0], last_trade=px[1])
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
            for r in self.flow.hot(300, FLOW_TOP_N):
                self._add(r["mint"], "flow", None, None, now)
        except Exception as e:
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    # ── разметка ──
    def label_tick(self, now: float | None = None) -> int:
        now = now or time.time()
        done = []
        with self.lock:
            items = list(self.pending.values())
        for smp in items:
            px = self.flow.price(smp["mint"])
            if px:
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
            for cp in CHECKPOINTS:
                if age >= cp and str(cp) not in smp["at"]:
                    smp["at"][str(cp)] = round(ret, 4)
            if age >= HORIZON_S:
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
                    out["hi"], out["lo"] = round(out["hi"], 4), round(out["lo"], 4)
                    out["dead_s"] = round(smp["t0"] + HORIZON_S - smp["last_trade"]) if smp["last_trade"] else None
                    fh.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    def _loop(self):
        while not self._stop.wait(TICK_S):
            try:
                self.label_tick()
            except Exception as e:      # разметка никогда не роняет сервер
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:160]

    def start(self):
        if self._thread is None and enabled():
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, name="learn", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        return dict(self.stats, pending=len(self.pending))


REC = Recorder()
