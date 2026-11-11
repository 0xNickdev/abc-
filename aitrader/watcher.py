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
import re
import threading
import time

import bot as botmod
import dexadapter
import geyser
import pumpcurve
import pumpflow
import wallets

# Smart-Exit Mirror: выходим, когда сливают смарт-кошельки, ради которых входили.
# ABC_SMART_EXIT=0 выключить; DROP — доля от базового баланса, ниже которой кошелёк
# считается «слившим» (0.5 = слил больше половины своей позиции).
SMART_EXIT = os.getenv("ABC_SMART_EXIT", "1").strip().lower() in ("1", "true", "yes", "on")
SMART_EXIT_DROP = float(os.getenv("ABC_SMART_EXIT_DROP", "0.35") or 0.35)

WATCH_POLL_S = float(os.getenv("ABC_WATCH_POLL_S", "2.0") or 2.0)
WS_ENABLED = os.getenv("ABC_WATCH_WS", "1").strip().lower() in ("1", "true", "yes", "on")
_MAX_WS_SUBS = 25
CURVE_RPC_EVERY_S = 10.0   # RPC-фолбэк цены кривой не чаще раза в N с на токен
REBASE_WINDOW_S = 120.0    # окно перебазирования цены входа на источник watcher'а
# Санити цены кривой (инцидент 11.10, Incognito): pump.fun-кривые с НЕ-SOL quote (SPCX и т.п.,
# 6 знаков вместо 9) дают цену в ~1000 раз ниже → мгновенный фальшивый «стоп −100%».
# Кривая показывает обвал > CURVE_SANITY_DROP от входа → сверяем с DexScreener; если тот
# в CURVE_SANITY_X+ раз выше — кривой по этому токену больше не верим (настоящий раг
# DexScreener тоже покажет, так что раги не прячутся).
CURVE_SANITY_DROP = 0.80
CURVE_SANITY_X = 5.0
# То же вверх: кривые в токенизированных акциях (TSLAx, NVDAx — 11.10) и других quote могут
# дать и завышенную цену → фальшивые иксы и тейки. Рост > CURVE_SANITY_UP раз от входа — сверка.
CURVE_SANITY_UP = 5.0
MIN_POLL_GAP_S = float(os.getenv("ABC_WATCH_MIN_GAP_S", "1.0") or 1.0)   # потолок частоты опроса
STALE_AFTER_WAIT_S = 1.5   # цена старше этого после ожидания замка сессии не применяется          # Helius лимит подписок на соединение держим с запасом


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
        self._curve_rpc_at: dict[str, float] = {}
        self._curve_bad: set[str] = set()          # токены, где цена кривой врёт (не-SOL quote)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        geyser.FEED._on_update = lambda mint: self._kick.set()   # событие кривой → цена сейчас
        pumpflow.FLOW.on_watch = self._flow_kick                 # сделка по нашей позиции в потоке → проверка сейчас
        geyser.FEED.start()
        threading.Thread(target=self._loop, daemon=True).start()
        if WS_ENABLED and ws_url():
            threading.Thread(target=self._ws_loop, daemon=True).start()

    def _flow_kick(self, mint: str):
        self.stats["flow_kicks"] = self.stats.get("flow_kicks", 0) + 1
        self._kick.set()

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

    def _sanity_check_curve(self, pairs, prices: dict, src: dict):
        """Обвал >80% или рост >5x по цене кривой → сверка с DexScreener; расходятся в разы — кривая врёт."""
        def odd(p):
            ep, cp = float(p.get("entry_price") or 0), float(prices.get(p["address"]) or 0)
            return ep > 0 and cp > 0 and (cp < (1 - CURVE_SANITY_DROP) * ep or cp > CURVE_SANITY_UP * ep)
        suspect = sorted({p["address"] for _, p in pairs
                          if src.get(p["address"]) in ("geyser", "curve-rpc") and odd(p)})
        if not suspect:
            return
        try:
            dx = dexadapter.spot_prices(suspect)
        except Exception:
            dx = {}
        for m in suspect:
            d, cp = float(dx.get(m) or 0), float(prices.get(m) or 0)
            if d > 0 and cp > 0 and max(d / cp, cp / d) >= CURVE_SANITY_X:   # расходятся в разы — кривая врёт
                self._curve_bad.add(m)
                prices[m], src[m] = d, "dex"
                self.stats["curve_rejected"] = self.stats.get("curve_rejected", 0) + 1

    # ── одна итерация (вынесена для тестов) ────────────────────────────────
    def poll_once(self):
        pairs = self._open_pairs()
        if not pairs:
            return
        mints = sorted({p["address"] for _, p in pairs})
        pumpflow.FLOW.watch = set(mints)
        # 0) Свой поток (24.10): цена главного пула по каждой сделке — без задержки DexScreener.
        #    PnL считаем относительно цены потока при входе (entry_flow_sol), источники не смешиваем.
        fpx = {m: v[0] for m in mints if (v := pumpflow.FLOW.price(m)) and v[0] > 0}
        anchored = {m for m in fpx if all(p.get("entry_flow_sol") for _, p in pairs if p["address"] == m)}
        # 1) Geyser (Shyft gRPC): токены на bonding curve — цена из резервов, обновление за
        #    десятки мс, без RPC-кредитов Helius. Авторитетный источник для кривой.
        geyser.FEED.set_mints(mints)
        prices = {m: v for m, v in geyser.FEED.prices_usd(mints).items() if m not in self._curve_bad}
        src = {m: "geyser" for m in prices}
        # 2) DexScreener — для мигрировавших (AMM) и всего, чего нет в Geyser
        rest = [m for m in mints if m not in prices and m not in anchored]
        if rest:
            try:
                dx = dexadapter.spot_prices(rest)
            except Exception:
                dx = {}
            prices.update(dx)
            src.update({m: "dex" for m in dx})
        # 3) Запасной путь: кривая по RPC — для pump-токенов без цены (Geyser лежит ИЛИ по токену
        #    ещё не было сделки с момента подписки: Yellowstone не шлёт стартовый снимок).
        #    Не чаще раза в CURVE_RPC_EVERY_S на токен — Helius-кредиты не жжём.
        now_m = time.monotonic()
        missing = [m for m in mints if not prices.get(m) and m.endswith("pump") and m not in self._curve_bad
                   and now_m - self._curve_rpc_at.get(m, 0.0) >= CURVE_RPC_EVERY_S]
        for m in missing:
            self._curve_rpc_at[m] = now_m
        if missing:
            try:
                got = pumpcurve.usd_prices(missing)
                prices.update(got)
                src.update({m: "curve-rpc" for m in got})
                self.stats["curve_prices"] = self.stats.get("curve_prices", 0) + len(got)
            except Exception:
                pass
        self._sanity_check_curve(pairs, prices, src)
        if not prices and not fpx:
            return                                  # оба источника легли — ждём следующего круга
        self.stats["polls"] += 1
        fetched_at = time.monotonic()
        own_s = 0.0                                  # время наших же продаж в этом круге
        for sess, p in pairs:
            px = float(prices.get(p["address"]) or 0.0)
            ep = float(p.get("entry_price", 0.0) or 0.0)
            fp = fpx.get(p["address"])
            if fp and ep > 0 and src.get(p["address"]) != "geyser":
                ef = float(p.get("entry_flow_sol") or 0)
                if not ef:
                    # якорь: у новой позиции — текущая цена потока (вход только что), у старой —
                    # подгоняем под текущий PnL по DexScreener, чтобы не обнулить его
                    young = p.get("opened_ts") and time.time() - float(p["opened_ts"]) < REBASE_WINDOW_S
                    mid_px = float((p.get("sim_entry") or {}).get("mid") or 0)
                    if young or px <= 0:
                        # 25.10: вход платил издержки (entry_price > рыночной mid) — якорь так, чтобы
                        # PnL стартовал с −издержек, а не с нуля (иначе результат приукрашен)
                        ef = fp * (ep / mid_px) if mid_px > 0 else fp
                    else:
                        ef = fp / (px / ep)
                    p["entry_flow_sol"] = ef
                px = ep * fp / ef
                src[p["address"]] = "flow"
            if px <= 0 or ep <= 0:
                continue
            with sess.lock:
                # цена снята ДО ожидания замка: если ждали ЧУЖОЙ замок долго — она уже не
                # «свежая». Свои медленные продажи в этом круге не в счёт (аудит 10.10: одна
                # LIVE-продажа на 12с выбивала все остальные позиции круга при общем дампе).
                if time.monotonic() - fetched_at - own_s > STALE_AFTER_WAIT_S:
                    self.stats["stale_skips"] = self.stats.get("stale_skips", 0) + 1
                    continue
                if p not in sess.positions:         # продана параллельным тиком бота
                    continue
                # Перебазирование входа (аудит 28.09/10.10): цена входа бралась из GMGN, выход —
                # из кривой/DexScreener → фальшивые ×45/×94. Первая RT-цена в первые
                # REBASE_WINDOW_S после входа, расходящаяся >30%, становится ценой входа.
                s_ = src.get(p["address"])
                if (not p.get("entry_src") and s_ and p.get("opened_ts")
                        and time.time() - float(p["opened_ts"]) < REBASE_WINDOW_S):
                    p["entry_src"] = s_
                    # ТОЛЬКО вверх: фальшивые иксы были от заниженной цены входа. Реальный обвал
                    # в первые секунды (раг) перебазированием прятать нельзя — это убыток.
                    if px / ep - 1.0 > 0.30 and p.get("entry_src_fill") is None:
                        p["entry_price_orig"] = ep
                        p["entry_price"] = ep = px
                        p.pop("peak_pnl", None)      # пик, посчитанный от фальшивого входа, — в мусор
                        self.stats["entry_rebased"] = self.stats.get("entry_rebased", 0) + 1
                p["px_src"] = s_
                p["cur_price"] = px
                p["pnl"] = round((px - ep) / ep, 4)
                if "peak_pnl" not in p or p["pnl"] > float(p["peak_pnl"]):
                    p["peak_ts"] = time.time()   # когда был максимум (выход stall: N минут без нового максимума)
                p["peak_pnl"] = max(float(p.get("peak_pnl", p["pnl"])), p["pnl"])
                p["rt_ts"] = time.time()   # штамп свежести: monitor_positions не перетирает
                self.stats["price_updates"] += 1   # RT-цену устаревшей строкой хот-листа
                # авто-выходы: позиции бота (флаг auto на позиции — стопы живут, даже если бот
                # остановили/рестартнули) или включённый бот; Phantom (self_custody без session)
                # продать может только владелец — ему алерт о пробитом стопе.
                auto = p.get("auto") or getattr(sess.bot, "enabled", False)
                rc = sess.risk_cfg() if hasattr(sess, "risk_cfg") else self._risk_cfg   # свой риск тестера
                if p.get("self_custody") and not p.get("session"):
                    if p["pnl"] <= -float(rc.get("hard_stop_pct", 0.25)) and not p.get("stop_alerted"):
                        p["stop_alerted"] = True
                        try:
                            import notify
                            notify.send(f"STOP HIT {p.get('symbol', '?')} {p['pnl']:+.0%} — sell in Phantom")
                        except Exception:
                            pass
                    continue
                if not auto:
                    continue
                p.setdefault("tp_taken", [])
                if p.get("market") == "mid":         # признаки выхода по потоку: крупный слив, мёртвый токен
                    p["flow_shock"] = pumpflow.FLOW.shock_since(p["address"], float(p.get("opened_ts") or 0))
                    p["flow_dead"] = pumpflow.FLOW.dead_stats(p["address"])
                elif p.get("market") == "breakout":   # выходы по потоку: крупный слив, тишина, слив за 5 мин
                    p["flow_shock"] = pumpflow.FLOW.shock_since(p["address"], float(p.get("opened_ts") or 0))
                    p["flow_drain"] = pumpflow.FLOW.drain(p["address"], 5)
                elif p.get("market") == "migration":  # выход: токен мигрировал / крупный слив на кривой
                    p["flow_shock"] = pumpflow.FLOW.shock_since(p["address"], float(p.get("opened_ts") or 0))
                    p["migrated"] = pumpflow.FLOW.graduated(p["address"])
                ed = botmod.decide_exit(p, None, rc, sess.bot.cfg)
                if ed.action != "SELL":
                    continue
                partial = ed.rung >= 0 and ed.fraction < 1.0
                if partial:
                    p["tp_taken"].append(ed.rung)   # до продажи (do_sell пишет диск), откат ниже
                t_sell = time.monotonic()
                try:
                    res = self._sell_for(sess)(p["address"], fraction=ed.fraction,
                                               reason="RT " + ed.reason)
                    own_s += time.monotonic() - t_sell
                except Exception as e:              # 404 гонка с ботом и т.п. — не роняем цикл
                    own_s += time.monotonic() - t_sell
                    if partial:
                        p["tp_taken"].remove(ed.rung)
                    self._log("WATCH_ERR", p.get("symbol", "?"), str(e))
                    continue
                if isinstance(res, dict) and res.get("proposed"):
                    if partial:                     # N1: лишь предложение — ступень не взята
                        p["tp_taken"].remove(ed.rung)
                    continue
                self.stats["exits"] += 1

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
                   if (p.get("auto") or getattr(s.bot, "enabled", False))
                   and (not p.get("self_custody") or p.get("session"))
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
                try:
                    res = self._sell_for(sess)(p["address"], fraction=1.0,
                                               reason=f"SMART-EXIT insiders dumping {dumped}/{total}")
                    p["smart_exit_done"] = True            # один выстрел — после успешной отправки
                    if not (isinstance(res, dict) and res.get("proposed")):
                        self.stats["smart_exits"] = self.stats.get("smart_exits", 0) + 1
                except Exception as e:                     # не удалось — повторим на следующем круге
                    self._log("WATCH_ERR", p.get("symbol", "?"), f"smart-exit: {e}")

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
                self.smart_exit_once()
            except Exception:
                pass
            timeout = WATCH_POLL_S if self._open_pairs() else 5.0
            self._kick.wait(timeout)
            self._kick.clear()
            # WS-пинок будит сразу, но не чаще MIN_POLL_GAP_S: на активном токене Helius шлёт
            # десятки событий в секунду (прод 29.09: 73k пинков → ~3.7 опроса/с — у порога
            # DexScreener 300/мин и жжёт RPC-кредиты на curve/smart-exit).
            gap = MIN_POLL_GAP_S - (time.monotonic() - started)
            if gap > 0:
                self._stop.wait(gap)

    # ── WS-пинок: Helius logsSubscribe по минтам позиций ───────────────────
    def _ws_loop(self):
        import asyncio

        async def run():
            import websockets
            backoff = 0.0
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
                        backoff = 0.0                     # соединение живое — бэкофф сначала
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
                except Exception as e:
                    self.stats["ws_state"] = "reconnecting"
                    self.stats["ws_reconnects"] = self.stats.get("ws_reconnects", 0) + 1
                    # причина в /api/status (ключ из URL вырезаем) — раньше глоталась молча
                    self.stats["ws_error"] = re.sub(r"(api[-_]?key=)[^&\s\"']+", r"\1***",
                                                    f"{type(e).__name__}: {e}")[:200]
                    # экспоненциальный бэкофф: фиксированные 3с давали 147k реконнектов и
                    # 429 от Helius на весь ключ (прод 10.10); 3→6→…→300с, сброс после успеха
                    backoff = min(300.0, backoff * 2) if backoff else 3.0
                    await asyncio.sleep(backoff)

        try:
            asyncio.run(run())
        except Exception:
            self.stats["ws_state"] = "dead"               # WS — ускоритель, poll живёт и без него


_started: PositionWatcher | None = None
_start_lock = threading.Lock()


def stats() -> dict | None:
    """Снимок статистики контура для /api/status (None = не запущен)."""
    if not _started:
        return None
    s = dict(_started.stats)
    s["geyser"] = geyser.FEED.snapshot() if geyser.enabled() else "off"
    if geyser.enabled() and geyser.pumpflow.enabled():
        s["flow"] = dict(geyser.pumpflow.FLOW.snapshot(), compare=geyser.pumpflow.COMPARE.summary())
    else:
        s["flow"] = "off"
    return s


def ensure_started(sessions_fn, risk_cfg, sell_fn_factory, log_fn=None) -> PositionWatcher:
    """Идемпотентный запуск (reload/линза тестов не плодят нити)."""
    global _started
    with _start_lock:
        if _started is None:
            _started = PositionWatcher(sessions_fn, risk_cfg, sell_fn_factory, log_fn)
            _started.start()
        return _started
