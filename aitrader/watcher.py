"""Real-time монитор открытых позиций: стопы без 20-секундных дыр.

Зачем: бот проверяет выходы раз в poll_s (20с), а цена позиции обновлялась из хот-листа
GMGN — на дампе это давало закрытия −44…−51% при стопе −35%. Здесь отдельный контур:

  1) Poll-цикл: раз в ~WATCH_POLL_S тянем цены ВСЕХ открытых sol-позиций одним батч-
     запросом DexScreener (бесплатно, без ключа) → обновляем cur_price/pnl/peak_pnl.
  2) WS-пинок (опционально): Helius logsSubscribe по минтам позиций — любая транзакция
     по токену будит цикл немедленно (реакция ~1-2с вместо фиксированных 2с). Сами логи
     не парсим: событие = «по токену что-то происходит» → обновить цену. Единицы цен
     не смешиваются (везде USD DexScreener), реконнект при обрыве/смене состава позиций.
  3) Выходы исполняются ТОЛЬКО для сессий с запущенным ботом (авто-режим) — теми же
     правилами bot.decide_exit (hard-stop / trailing / TP-ладder; escape-severity остаётся
     на тике бота — тут нет security-данных). Ручные позиции человек закрывает сам;
     self_custody не трогаем никогда (продать может только владелец подписью в Phantom).

Обе нити — daemon; любой сбой цикла не роняет процесс (лог + продолжаем).
"""
from __future__ import annotations

import json
import os
import threading
import time

import bot as botmod
import dexadapter
import pumpcurve
import wallets

# Smart-Exit Mirror: выходим, когда сливают смарт-кошельки, ради которых входили.
# ABC_SMART_EXIT=0 выключить; DROP — доля от базового баланса, ниже которой кошелёк
# считается «слившим» (0.5 = слил больше половины своей позиции).
SMART_EXIT = os.getenv("ABC_SMART_EXIT", "1").strip().lower() in ("1", "true", "yes", "on")
SMART_EXIT_DROP = float(os.getenv("ABC_SMART_EXIT_DROP", "0.35") or 0.35)

WATCH_POLL_S = float(os.getenv("ABC_WATCH_POLL_S", "2.0") or 2.0)
WS_ENABLED = os.getenv("ABC_WATCH_WS", "1").strip().lower() in ("1", "true", "yes", "on")
_MAX_WS_SUBS = 25          # Helius лимит подписок на соединение держим с запасом


def ws_url() -> str:
    """wss-эндпоинт: HELIUS_WS_URL напрямую, иначе выводим из SOLANA_RPC_URL (helius)."""
    u = os.getenv("HELIUS_WS_URL", "").strip()
    if u:
        return u
    rpc = os.getenv("SOLANA_RPC_URL", "").strip()
    if "helius" in rpc and rpc.startswith("http"):
        return "wss://" + rpc.split("://", 1)[1]
    return ""


class PositionWatcher:
    """sessions_fn() -> list[UserSession]; sell_fn_factory(sess) -> callable(address, fraction=, reason=).
    risk_cfg = app.CFG (hard_stop_pct/trailing_pct/tp_ladder — те же правила, что у бота)."""

    def __init__(self, sessions_fn, risk_cfg, sell_fn_factory, log_fn=None):
        self._sessions = sessions_fn
        self._risk_cfg = risk_cfg
        self._sell_for = sell_fn_factory
        self._log = log_fn or (lambda *a, **k: None)
        self._kick = threading.Event()
        self._stop = threading.Event()
        self.stats = dict(polls=0, price_updates=0, exits=0, ws_kicks=0, ws_state="off")

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        if WS_ENABLED and ws_url():
            threading.Thread(target=self._ws_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._kick.set()

    # ── позиции ────────────────────────────────────────────────────────────
    def _open_pairs(self):
        out = []
        for s in self._sessions():
            for p in s.positions:
                if p.get("chain", "sol") == "sol":
                    out.append((s, p))
        return out

    def _mints(self) -> list[str]:
        return sorted({p["address"] for _, p in self._open_pairs()})

    # ── одна итерация (вынесена для тестов) ────────────────────────────────
    def poll_once(self):
        pairs = self._open_pairs()
        if not pairs:
            return
        mints = sorted({p["address"] for _, p in pairs})
        try:
            prices = dexadapter.spot_prices(mints)
        except Exception:
            prices = {}
        # Свежие pump.fun-токены до индексации DexScreener: цена напрямую из bonding curve
        # (RPC) — watcher видит токен с первой секунды, стоп не слепнет на самых молодых.
        missing = [m for m in mints if not prices.get(m) and m.endswith("pump")]
        if missing:
            try:
                got = pumpcurve.usd_prices(missing)
                prices.update(got)
                self.stats["curve_prices"] = self.stats.get("curve_prices", 0) + len(got)
            except Exception:
                pass
        if not prices:
            return                                  # оба источника легли — ждём следующего круга
        self.stats["polls"] += 1
        for sess, p in pairs:
            px = float(prices.get(p["address"]) or 0.0)
            ep = float(p.get("entry_price", 0.0) or 0.0)
            if px <= 0 or ep <= 0:
                continue
            with sess.lock:
                if p not in sess.positions:         # продана параллельным тиком бота
                    continue
                p["cur_price"] = px
                p["pnl"] = round((px - ep) / ep, 4)
                p["peak_pnl"] = max(float(p.get("peak_pnl", p["pnl"])), p["pnl"])
                p["rt_ts"] = time.time()   # штамп свежести: monitor_positions не перетирает
                self.stats["price_updates"] += 1   # RT-цену устаревшей строкой хот-листа
                # авто-выходы: только бот-сессии, только не self-custody
                if not getattr(sess.bot, "enabled", False) or p.get("self_custody"):
                    continue
                p.setdefault("tp_taken", [])
                ed = botmod.decide_exit(p, None, self._risk_cfg, sess.bot.cfg)
                if ed.action != "SELL":
                    continue
                try:
                    self._sell_for(sess)(p["address"], fraction=ed.fraction,
                                         reason="RT " + ed.reason)
                    if ed.rung >= 0 and ed.fraction < 1.0:
                        p["tp_taken"].append(ed.rung)
                    self.stats["exits"] += 1
                except Exception as e:              # 404 гонка с ботом и т.п. — не роняем цикл
                    self._log("WATCH_ERR", p.get("symbol", "?"), str(e))

    # ── Smart-Exit Mirror: инсайдеры (кошельки из атрибуции входа) сливают → выходим ──
    def _insider_atas(self, p: dict) -> list[str]:
        """ATA отслеживаемых кошельков из атрибуции входа (кэш в позиции).
        Имена из entry_attrib.tracked резолвятся в адреса через реестр wallets.TRACKED."""
        if "_ins_atas" in p:
            return p["_ins_atas"]
        names = set((p.get("entry_attrib") or {}).get("tracked") or [])
        atas = []
        if names:
            for addr, w in wallets.TRACKED.items():
                if w.get("name") in names:
                    try:
                        atas.append(pumpcurve.ata_address(addr, p["address"]))
                    except Exception:
                        continue
        p["_ins_atas"] = atas
        return atas

    def smart_exit_once(self):
        """Один проход зеркала: балансы инсайдеров по нашим позициям одним батчом;
        ≥половина инсайдеров слила >DROP своей позиции → выходим (только бот-сессии)."""
        if not SMART_EXIT:
            return
        targets = [(s, p) for s, p in self._open_pairs()
                   if getattr(s.bot, "enabled", False) and not p.get("self_custody")
                   and not p.get("smart_exit_done") and self._insider_atas(p)]
        if not targets:
            return
        all_atas = sorted({a for _, p in targets for a in p["_ins_atas"]})
        try:
            bal = pumpcurve.token_amounts(all_atas)
        except Exception:
            return
        for sess, p in targets:
            with sess.lock:
                if p not in sess.positions:
                    continue
                base = p.setdefault("insider_base", {})
                dumped = total = 0
                for ata in p["_ins_atas"]:
                    cur = bal.get(ata)
                    if cur is None:
                        continue
                    if ata not in base:
                        base[ata] = cur                    # базовый баланс на первом наблюдении
                    if base[ata] <= 0:
                        continue
                    total += 1
                    if cur < base[ata] * (1.0 - SMART_EXIT_DROP):
                        dumped += 1
                if total == 0 or dumped < max(1, (total + 1) // 2):
                    continue
                p["smart_exit_done"] = True                # один выстрел на позицию
                try:
                    self._sell_for(sess)(p["address"], fraction=1.0,
                                         reason=f"SMART-EXIT insiders dumping {dumped}/{total}")
                    self.stats["smart_exits"] = self.stats.get("smart_exits", 0) + 1
                except Exception as e:
                    self._log("WATCH_ERR", p.get("symbol", "?"), f"smart-exit: {e}")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.smart_exit_once()
            except Exception:
                pass
            timeout = WATCH_POLL_S if self._open_pairs() else 5.0
            self._kick.wait(timeout)
            self._kick.clear()

    # ── WS-пинок: Helius logsSubscribe по минтам позиций ───────────────────
    def _ws_loop(self):
        import asyncio

        async def run():
            import websockets
            while not self._stop.is_set():
                mints = self._mints()[:_MAX_WS_SUBS]
                if not mints:
                    self.stats["ws_state"] = "idle"
                    await asyncio.sleep(5.0)
                    continue
                want = frozenset(mints)
                try:
                    async with websockets.connect(ws_url(), ping_interval=20,
                                                  close_timeout=5) as ws:
                        for i, m in enumerate(mints):
                            await ws.send(json.dumps({
                                "jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                "params": [{"mentions": [m]}, {"commitment": "confirmed"}]}))
                        self.stats["ws_state"] = f"subscribed:{len(mints)}"
                        while not self._stop.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                                if "logsNotification" in msg:
                                    self.stats["ws_kicks"] += 1
                                    self._kick.set()      # активность по токену → цену сейчас
                            except asyncio.TimeoutError:
                                pass                      # тишина — просто проверяем состав
                            if frozenset(self._mints()[:_MAX_WS_SUBS]) != want:
                                break                     # позиции сменились → переподписка
                except Exception:
                    self.stats["ws_state"] = "reconnecting"
                    await asyncio.sleep(3.0)              # обрыв/бан — тихий реконнект

        try:
            asyncio.run(run())
        except Exception:
            self.stats["ws_state"] = "dead"               # WS — ускоритель, poll живёт и без него


_started: PositionWatcher | None = None
_start_lock = threading.Lock()


def stats() -> dict | None:
    """Снимок статистики контура для /api/status (None = не запущен)."""
    return dict(_started.stats) if _started else None


def ensure_started(sessions_fn, risk_cfg, sell_fn_factory, log_fn=None) -> PositionWatcher:
    """Идемпотентный запуск (reload/линза тестов не плодят нити)."""
    global _started
    with _start_lock:
        if _started is None:
            _started = PositionWatcher(sessions_fn, risk_cfg, sell_fn_factory, log_fn)
            _started.start()
        return _started
