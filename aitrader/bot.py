"""ABC 量化机器人 —— 在既有风控/筛选之上的【自主执行层】，纸面优先（paper-first）。

定位：把"人一键买/卖"自动化成一个有纪律的执行回路，但【不另起一套风控】：
  • 入场：只对一轮 screen 里【通过全部闸门(ACTION) 且 ABC Alpha v1 触发】的候选自动建仓；
  • 离场：硬止损 > 逃生预警 > 移动止盈 > TP 阶梯分批（参数全取自 app.CFG，与人工口径一致）；
  • 纪律：每轮限新开仓数、不超并发、连亏熔断/当日亏损上限时只许平仓不许开仓。

安全（务必看清）：
  • 默认【关闭】，须显式 /api/bot/start 才跑；公开演示禁用。
  • 真假账完全沿用现有护栏：建仓/平仓都走 app.do_buy/do_sell，
    LIVE 且 ENABLE_LIVE_TRADING 解锁时才真正上链；否则一律 SHADOW 纸面记账，
    正好用来在 trade_decisions.jsonl 上攒 SELL 记录，喂 backtest.realized 出真胜率/R。
  • 撮合走我们自己的快速通道（执行期接 Jupiter Ultra+Beam），本模块只做"何时买/卖"的决策。

设计：纯决策函数（select_entries / decide_exit）不依赖 app，可单测；BotRunner 通过
依赖注入拿到 screen/buy/sell/positions/CFG/lock，避免与 app 形成循环 import。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

# Рынки, которые идут через свой поток (pumpflow), а не через GMGN-скан.
FLOW_MARKETS = ("mid", "snipe", "migration", "breakout")
# Рынки на обкатке: в LIVE вход закрыт, пока бумага не покажет плюс.
PAPER_ONLY_MARKETS = ("migration", "breakout")

# 机器人自身的执行参数（与 strategy.PRESET / app.CFG 的风控参数互补，不重复其值）。
CFG = {
    "mode": "n2",                 # n1=предлагает, человек подтверждает · n2=исполняет сам (paper/LIVE-замки)
    "max_new_per_tick": 2,        # 每轮最多新开仓数（防一轮梭哈）
    "poll_s": 20.0,               # 自主轮询间隔（秒）
    "escape_severity_exit": 60,   # 逃生严重度 ≥ 此值即清仓离场（比人工 escape_severity 略激进）
    "trail_activate_pct": 0.30,   # 浮盈达此幅度后才启用移动止盈（避免刚建仓就被噪声扫出）
    "require_abc_trigger": True,  # 只对 ABC Alpha v1 触发的候选自动建仓
    # 最低优先级分才自主建仓（0=关闭；качество: ~70）. Env-дефолт нужен house-боту
    # (BOT_AUTOSTART): его cfg живёт в памяти и слетает при рестарте контейнера —
    # ABC_BOT_MIN_PRIORITY на Railway переживает деплой.
    "min_priority": float(os.getenv("ABC_BOT_MIN_PRIORITY", "0") or 0),
    # Анализ перевеса 10.10 (2 450 позиций, проверка на отложенной половине): единственная
    # фича, монотонно связанная с результатом во всех разрезах — buy_ratio. ≥0.65: +10…16%/сделку
    # после 7% издержек в тесте; <0.60: −14%. sm_confluence > 8 — одна из худших корзин.
    # priority/liquidity/chg_5m/возраст перевеса не дали. 0 = выключить фильтр.
    "min_buy_ratio": float(os.getenv("ABC_BOT_MIN_BUY_RATIO", "0.65") or 0),
    # Ёмкость дампа (12.10, «давай попробуем»; откат — тег rollback-2026-10-12-pre-dump): не входить,
    # если топ-10 держателей, продав разом, обвалят цену сильнее порога (%). Стоп −20% исполнялся
    # на −40…−100%: дамп — одна tx, цена перепрыгивает стоп. 0 = выкл; токены без полной истории проходят.
    "max_dump_pct": float(os.getenv("ABC_BOT_MAX_DUMP_PCT", "50") or 0),
    # Рынок бота (16.10, юзер: «переводи»): "mid" — токены от $200k моложе суток (как HOTBOT;
    # симуляция 15–16.10: +0.6…+3.9%/сделку против −14…−20% у свежих pump.fun), "fresh" — старый
    # скан свежих токенов через GMGN. Выходы mid (23.10, как HOTBOT): на +100% — продать 50% на откате
    # 15% от пика, остаток — трейлинг-стоп; до первой фиксации — стоп −20% и выход по времени 4 ч.
    "market": os.getenv("ABC_BOT_MARKET", "mid").strip().lower() or "mid",
    "mid_sl": 0.20, "mid_max_hold_s": 4 * 3600,
    "mid_tp1": 1.00, "mid_tp1_frac": 0.50, "mid_tp1_pullback": 0.15, "mid_trail": 0.25,
    # 24.10 (стопы −47/−66% на рагах): выход сразу после крупной продажи одной транзакцией
    # (цена −15% или ушло ≥25% SOL пула) и из «мёртвого» токена — за 30 мин оборот <0.5% капы и
    # размах цены <4% (оба рага стояли так перед сливом). Признаки пишет монитор по нашему потоку.
    "mid_shock_drop": float(os.getenv("ABC_MID_SHOCK_DROP", "0.15") or 0),
    "mid_shock_pool": float(os.getenv("ABC_MID_SHOCK_POOL", "0.25") or 0),
    "mid_dead_turnover": float(os.getenv("ABC_MID_DEAD_TURNOVER", "0.005") or 0),
    "mid_dead_range": float(os.getenv("ABC_MID_DEAD_RANGE", "0.04") or 0),
    "mid_dead_hold_s": 1800,
    # ранний рынок (25.10, рецепт HOTBOT clip): не вырос за N мин → выходим у безубытка,
    # не досиживаем мёртвый коин до 4ч. peak_pnl < порога за время = «не поехал».
    "mid_breakeven_min": float(os.getenv("ABC_MID_BREAKEVEN_MIN", "10") or 0),
    "mid_breakeven_rise": float(os.getenv("ABC_MID_BREAKEVEN_RISE", "0.12") or 0),
    "max_sm_confluence": int(os.getenv("ABC_BOT_MAX_SM", "8") or 0),
    # Migration bot (11.11): купили на кривой перед миграцией → продаём, как только токен
    # мигрировал на PumpSwap; не дошёл — стоп или выход по времени (проверка 08.11: стоп 15%, 20 мин).
    "mig_sl": float(os.getenv("ABC_MIG_SL", "0.15") or 0.15),
    "mig_max_hold_s": float(os.getenv("ABC_MIG_MAX_HOLD_S", "1200") or 1200),
    # Breakout bot (11.11, механика HOTBOT): вход на пробое тихого диапазона, выход — именованный план
    # (EXIT_PLANS) над всегда включённым трейлингом от максимума; плюс выходы по тишине и сливу.
    "brk_plan": os.getenv("ABC_BRK_PLAN", "clip").strip() or "clip",
    "brk_trail": float(os.getenv("ABC_BRK_TRAIL", "0.20") or 0.20),
    "brk_no_trade_s": float(os.getenv("ABC_BRK_NO_TRADE_S", "300") or 0),
    "brk_drain_drop": float(os.getenv("ABC_BRK_DRAIN_DROP", "10") or 0),
    "brk_max_hold_s": float(os.getenv("ABC_BRK_MAX_HOLD_S", "14400") or 14400),
}

# Планы выхода HOTBOT (types/exits.ts, числа открытые). Доли — от исходной позиции.
#   tps: (рост, доля, откат) — тейк на росте; с откатом — после роста на откате от максимума
#   stops: trail(дистанция, arm=рост включения, after=минут удержания) | floor(after) — «по цене входа, если не вырос»
#   scale: (с роста, шаг, доля) — доля на росте и на каждом следующем шаге (сложным процентом)
#   lock: каждый шаг роста фиксирует ступень ниже · stall: (рост включения, минут без нового максимума)
def _trail(d, arm=0.0, after=0.0):
    return dict(kind="trail", dist=d, arm=arm, after=after)

def _floor(after):
    return dict(kind="floor", after=after)

EXIT_PLANS = {
    "clip": dict(name="Clip", tps=[(1.0, 1.0, 0)], stops=[_trail(0.10, arm=0.30), _floor(10)]),
    "stake": dict(name="Stake back", tps=[(1.0, 0.5, 0)], stops=[]),
    "flip": dict(name="Quick flip", tps=[(1.0, 1.0, 0)], stops=[]),
    "ladder": dict(name="Ladder", tps=[(0.5, 0.33, 0), (1.0, 0.33, 0)], stops=[]),
    "trailingTp": dict(name="Trailing take-profit", tps=[(1.0, 0.5, 0.15)], stops=[]),
    "trail": dict(name="Trailing stop only", tps=[], stops=[]),
    "scaleOut": dict(name="Scale out", tps=[], scale=(0.20, 0.10, 0.10), stops=[_floor(10)]),
    "scalp": dict(name="Quick scalp", tps=[(0.20, 0.5, 0)], stops=[_floor(5)]),
    "take30": dict(name="Take 30%", tps=[(0.30, 1.0, 0)], stall=(0.15, 10), stops=[_floor(20)]),
    "stall": dict(name="Stall exit", tps=[], stall=(0.15, 5), stops=[_floor(10)]),
    "stepLock": dict(name="Step lock", tps=[], lock=0.25, stops=[_floor(10)]),
    "doubling": dict(name="Doubling ladder", tps=[], scale=(1.0, 1.0, 0.34), stops=[]),
    "tighten": dict(name="Tightening trail", tps=[], stops=[_floor(10), _trail(0.15, after=10), _trail(0.08, after=30)]),
}


def _plan_tp(plan: dict, rung: int):
    """Тейк номер rung: сначала свои тейки плана, затем ступени scale, пока не продана вся позиция."""
    tps = plan.get("tps") or []
    if rung < len(tps):
        return tps[rung]
    sc, step = plan.get("scale"), rung - len(tps)
    if not sc or step * sc[2] >= 1.0:
        return None
    return ((1 + sc[0]) * (1 + sc[1]) ** step - 1, sc[2], 0)


def plan_exit(pos: dict, plan_id: str, trail: float = 0.0, now: float | None = None) -> "ExitDecision":
    """Выход по именованному плану над всегда включённым трейлингом `trail` (0 = без него).
    pos: pnl, peak_pnl, opened_ts, tp_taken (номера взятых тейков), peak_ts (время максимума)."""
    plan = EXIT_PLANS.get(plan_id) or EXIT_PLANS["clip"]
    now = now or time.time()
    pnl = float(pos.get("pnl", 0.0))
    peak = max(float(pos.get("peak_pnl", pnl)), pnl, 0.0)   # максимум с момента покупки — не ниже цены входа
    taken = pos.get("tp_taken", []) or []
    held_min = (now - float(pos.get("opened_ts") or now)) / 60
    name = plan["name"]
    # уровень стопа (в долях цены входа): побеждает самый высокий из активных
    level, why = 0.0, ""
    stops = list(plan.get("stops") or [])
    if trail > 0:
        stops.insert(0, _trail(trail))
    for st in stops:
        if st.get("after") and held_min < st["after"]:
            continue
        if st["kind"] == "floor":
            lv, w = 1.0, f"not up {st['after']:.0f}m after the buy"
        else:
            if st.get("arm") and peak < st["arm"]:
                continue
            lv, w = (1 + peak) * (1 - st["dist"]), f"trailing stop {st['dist']:.0%} below the high (peak {peak:+.0%})"
        if lv > level:
            level, why = lv, w
    lock = plan.get("lock")
    if lock:
        steps = int(peak / lock + 1e-9)
        if steps >= 2 and 1 + (steps - 1) * lock > level:
            level, why = 1 + (steps - 1) * lock, f"step lock holds +{(steps - 1) * lock:.0%}"
    if level > 0 and 1 + pnl <= level:
        return ExitDecision("SELL", 1.0, f"{name}: {why}, PnL {pnl:+.0%}")
    stall = plan.get("stall")
    if stall and peak >= stall[0] and pos.get("peak_ts") and now - float(pos["peak_ts"]) >= stall[1] * 60:
        return ExitDecision("SELL", 1.0, f"{name}: no new high for {stall[1]:.0f}m after +{stall[0]:.0%}, PnL {pnl:+.0%}")
    rung = len(taken)
    tp = _plan_tp(plan, rung)
    if tp:
        gain, share, pullback = tp
        drawdown = 1.0 - (1.0 + pnl) / (1.0 + peak) if peak > -1.0 else 0.0
        hit = (peak >= gain and drawdown >= pullback) if pullback else pnl >= gain
        if hit:
            sold = sum((_plan_tp(plan, i) or (0, 0, 0))[1] for i in range(rung))
            left = max(1e-9, 1.0 - sold)
            frac = 1.0 if sold + share >= 0.999 else min(1.0, share / left)
            return ExitDecision("SELL", round(frac, 4), f"{name}: take {share:.0%} at +{gain:.0%}, PnL {pnl:+.0%}",
                                rung=-1 if frac >= 1.0 else rung)
    return ExitDecision("HOLD", 0.0, "")


@dataclass
class ExitDecision:
    action: str          # "SELL" | "HOLD"
    fraction: float      # 清仓比例 0..1（TP 阶梯为分批，其余为 1.0 全清）
    reason: str
    rung: int = -1       # 命中的 TP 阶梯档位下标（仅分批离场用，供调用方标记已兑现）


def select_entries(decisions, held_addrs, n_open, max_concurrent, max_new,
                   *, require_trigger: bool = True, min_priority: float = 0.0,
                   min_buy_ratio: float = 0.0, max_sm_confluence: int = 0, max_dump_pct: float = 0.0):
    """从一轮 screen 结果挑【可自主建仓】的候选。

    条件：通过全部闸门（decision.action=="ACTION"）+ ABC 触发 + 未持有 + 不超并发。
    返回 [(address, size_sol, symbol), ...]，按 ABC score 降序，取 min(空位, max_new)。
    """
    slots = max(0, int(max_concurrent) - int(n_open))
    if slots <= 0:
        return []
    cands = []
    for d in decisions or []:
        dec = d.get("decision", {}) if isinstance(d, dict) else {}
        if dec.get("action") != "ACTION":
            continue
        addr = dec.get("address")
        if not addr or addr in held_addrs:
            continue
        abc = dec.get("abc") or {}
        if require_trigger and not abc.get("triggered"):
            continue
        if min_priority and float(dec.get("priority", 0)) < min_priority:
            continue                                  # качество: пропускаем слабые сетапы
        feats = dec.get("features") or {}
        if min_buy_ratio and float(feats.get("buy_ratio", 0) or 0) < min_buy_ratio:
            continue                                  # перевес только при доминировании покупок
        if max_sm_confluence and int(feats.get("sm_confluence", 0) or 0) > max_sm_confluence:
            continue                                  # «все умники уже внутри» — поздний вход
        dump = feats.get("dump_top10_pct")
        if max_dump_pct and dump is not None and float(dump) > max_dump_pct:
            continue                                  # топ-10 держателей могут обвалить цену сильнее порога
        size = dec.get("size_sol", 0.0)
        if not size or size <= 0:
            continue
        cands.append((float(abc.get("score", 0.0)), addr, float(size), dec.get("symbol", "")))
    cands.sort(key=lambda x: -x[0])
    return [(addr, size, sym) for _s, addr, size, sym in cands[:min(slots, int(max_new))]]


def decide_exit(pos: dict, severity, risk_cfg: dict, bot_cfg: dict | None = None) -> ExitDecision:
    """对一个持仓判离场。优先级：硬止损 > 逃生 > 移动止盈 > TP 阶梯分批。

    pos 须含 pnl（当前浮盈小数）、peak_pnl（历史峰值浮盈）、tp_taken（已兑现档位下标 list）。
    风控参数（hard_stop_pct / trailing_pct / tp_ladder）取自 risk_cfg（=app.CFG），与人工口径一致。
    """
    bc = bot_cfg or CFG
    pnl = float(pos.get("pnl", 0.0))
    peak = float(pos.get("peak_pnl", pnl))
    hard_stop = float(risk_cfg.get("hard_stop_pct", 0.35))
    trail = float(risk_cfg.get("trailing_pct", 0.25))
    ladder = risk_cfg.get("tp_ladder", []) or []
    taken = pos.get("tp_taken", []) or []

    if pos.get("market") == "migration":            # вход на кривой перед миграцией
        if pos.get("migrated"):
            return ExitDecision("SELL", 1.0, f"Migration exit: token graduated to PumpSwap, PnL {pnl:+.0%}")
        sh = pos.get("flow_shock")
        sd, sp = float(bc.get("mid_shock_drop", 0.15)), float(bc.get("mid_shock_pool", 0.25))
        if sh and ((sd > 0 and sh[1] >= sd) or (sp > 0 and sh[2] >= sp)):
            return ExitDecision("SELL", 1.0, f"Migration shock exit: one sell moved price -{sh[1]:.0%} "
                                             f"({sh[2]:.0%} of curve SOL), PnL {pnl:+.0%}")
        sl = float(bc.get("mig_sl", 0.15))
        if pnl <= -sl:
            return ExitDecision("SELL", 1.0, f"Migration stop PnL {pnl:+.0%} ≤ -{sl:.0%}")
        held = time.time() - float(pos.get("opened_ts") or time.time())
        if held >= float(bc.get("mig_max_hold_s", 1200)):
            return ExitDecision("SELL", 1.0, f"Migration time exit after {held / 60:.0f}m, PnL {pnl:+.0%}")
        return ExitDecision("HOLD", 0.0, "")
    if pos.get("market") == "breakout":             # вход на пробое: план выхода + выходы по потоку
        sh = pos.get("flow_shock")
        sd, sp = float(bc.get("mid_shock_drop", 0.15)), float(bc.get("mid_shock_pool", 0.25))
        if sh and ((sd > 0 and sh[1] >= sd) or (sp > 0 and sh[2] >= sp)):
            return ExitDecision("SELL", 1.0, f"Breakout shock exit: one sell moved price -{sh[1]:.0%} "
                                             f"({sh[2]:.0%} of pool SOL), PnL {pnl:+.0%}")
        dr = pos.get("flow_drain") or {}
        nt = float(bc.get("brk_no_trade_s", 300))
        if nt > 0 and dr.get("idle_s") is not None and dr["idle_s"] >= nt:
            return ExitDecision("SELL", 1.0, f"Breakout no-trade exit: no trades for {dr['idle_s']:.0f}s, PnL {pnl:+.0%}")
        dd = float(bc.get("brk_drain_drop", 10))
        if dd > 0 and dr.get("drop_pct") is not None and dr["drop_pct"] >= dd and dr.get("sells_over_buys"):
            return ExitDecision("SELL", 1.0, f"Breakout drain exit: -{dr['drop_pct']:.0f}% in 5m with more sells than buys, PnL {pnl:+.0%}")
        ed = plan_exit(pos, pos.get("exit_plan") or bc.get("brk_plan", "clip"), float(bc.get("brk_trail", 0.20)))
        if ed.action == "SELL":
            return ed
        held = time.time() - float(pos.get("opened_ts") or time.time())
        if held >= float(bc.get("brk_max_hold_s", 14400)) and not taken:
            return ExitDecision("SELL", 1.0, f"Breakout time exit after {held / 3600:.1f}h, PnL {pnl:+.0%}")
        return ExitDecision("HOLD", 0.0, "")
    if pos.get("market") in ("mid", "snipe"):       # рынок бота: выходы HOTBOT
        sl = float(bc.get("mid_sl", 0.20))
        sh = pos.get("flow_shock")                  # (ts, падение цены, доля пула) крупной продажи после входа
        sd, sp = float(bc.get("mid_shock_drop", 0.15)), float(bc.get("mid_shock_pool", 0.25))
        if sh and ((sd > 0 and sh[1] >= sd) or (sp > 0 and sh[2] >= sp)):
            return ExitDecision("SELL", 1.0, f"Mid shock exit: one sell moved price -{sh[1]:.0%} "
                                             f"({sh[2]:.0%} of pool SOL), PnL {pnl:+.0%}")
        dead = pos.get("flow_dead")                 # {turnover, range} за 30 мин по потоку
        held_s = time.time() - float(pos.get("opened_ts") or time.time())
        bmin = float(bc.get("mid_breakeven_min", 0))
        if bmin > 0 and held_s >= bmin * 60 and 0 not in taken and peak < float(bc.get("mid_breakeven_rise", 0.12)):
            return ExitDecision("SELL", 1.0, f"Mid breakeven exit: no +{float(bc.get('mid_breakeven_rise',0.12)):.0%} "
                                             f"in {bmin:.0f}m (peak {peak:+.0%}), PnL {pnl:+.0%}")
        dt_, dr = float(bc.get("mid_dead_turnover", 0.005)), float(bc.get("mid_dead_range", 0.04))
        if (dead and dt_ > 0 and held_s >= float(bc.get("mid_dead_hold_s", 1800))
                and dead.get("turnover") is not None and dead.get("range") is not None
                and dead["turnover"] < dt_ and dead["range"] < dr):
            return ExitDecision("SELL", 1.0, f"Mid dead-token exit: 30m turnover {dead['turnover']:.2%} of mcap, "
                                             f"range {dead['range']:.1%}, PnL {pnl:+.0%}")
        drawdown = 1.0 - (1.0 + pnl) / (1.0 + peak) if peak > -1.0 else 0.0
        if 0 in taken:                              # половина зафиксирована — остаток на трейлинге
            trail_m = float(bc.get("mid_trail", 0.25))
            if drawdown >= trail_m:
                return ExitDecision("SELL", 1.0, f"Mid trailing stop: peak {peak:+.0%}, drawdown {drawdown:.0%} ≥ {trail_m:.0%}")
            return ExitDecision("HOLD", 0.0, "")
        if pnl <= -sl:
            return ExitDecision("SELL", 1.0, f"Mid stop PnL {pnl:+.0%} ≤ -{sl:.0%}")
        tp1, pb = float(bc.get("mid_tp1", 1.0)), float(bc.get("mid_tp1_pullback", 0.15))
        if peak >= tp1 and drawdown >= pb:
            frac = float(bc.get("mid_tp1_frac", 0.5))
            return ExitDecision("SELL", frac, f"Mid TP1: peak {peak:+.0%} ≥ +{tp1:.0%}, pullback {drawdown:.0%} — take {frac:.0%}",
                                rung=0)
        held = time.time() - float(pos.get("opened_ts") or time.time())
        if held >= float(bc.get("mid_max_hold_s", 14400)) and peak < tp1:
            return ExitDecision("SELL", 1.0, f"Mid time exit after {held / 3600:.1f}h, PnL {pnl:+.0%}")
        return ExitDecision("HOLD", 0.0, "")
    if pnl <= -hard_stop:
        return ExitDecision("SELL", 1.0, f"Hard stop PnL {pnl:+.0%} ≤ -{hard_stop:.0%}")
    if severity is not None and float(severity) >= bc["escape_severity_exit"]:
        return ExitDecision("SELL", 1.0, f"Escape exit severity {severity}≥{bc['escape_severity_exit']}")
    # Трейлинг — откат ЦЕНЫ от пика в %, а не «пункты PnL» (аудит 10.10: при пике +150% старая
    # формула выходила на откате цены 10%, при +300% — на 6%, раннер до TP3 не доживал).
    drawdown = 1.0 - (1.0 + pnl) / (1.0 + peak) if peak > -1.0 else 0.0
    if peak >= bc["trail_activate_pct"] and drawdown >= trail:
        return ExitDecision("SELL", 1.0, f"Trailing take-profit from peak {peak:+.0%} price drawdown {drawdown:.0%}≥{trail:.0%}")
    # TP 阶梯：命中尚未兑现的【最低未取】档，分批落袋（一轮只取一档，避免一次扫光）
    for i, rung in enumerate(ladder):
        if i in taken:
            continue
        gain, frac = rung
        if pnl >= float(gain):
            return ExitDecision("SELL", float(frac), f"TP{i + 1} hit +{float(gain):.0%}, take {float(frac):.0%}", rung=i)
    return ExitDecision("HOLD", 0.0, "")


class BotRunner:
    """自主执行回路。start() 注入依赖并起后台线程；stop() 停。tick() 可单测/手动调一轮。"""

    def __init__(self):
        self.enabled = False
        self.chain = "sol"
        self.cfg = dict(CFG)
        self.stats = dict(ticks=0, buys=0, sells=0, partials=0, blocked=0,
                          errors=0, last_tick=None, last_error=None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fns: dict | None = None

    # ── 生命周期 ───────────────────────────────────────────────────────────
    def start(self, chain: str, *, screen_fn, buy_fn, sell_fn, positions_fn,
              risk_cfg, lock, halted_fn=None, max_positions_fn=None) -> bool:
        """注入 app 的能力并启动。已在跑则返回 False（幂等）。"""
        if self.enabled:
            return False
        self.chain = chain
        self._fns = dict(screen=screen_fn, buy=buy_fn, sell=sell_fn,
                         positions=positions_fn, risk_cfg=risk_cfg, lock=lock,
                         halted=halted_fn or (lambda: False),
                         max_positions=max_positions_fn)
        self.enabled = True
        # Своё Event на каждый запуск: stop→start подряд раньше очищал общий _stop раньше,
        # чем старая нить его видела → крутились ДВА цикла (двойная нагрузка на GMGN/входы).
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        self.enabled = False
        self._stop.set()
        return True

    def _loop(self, stop_ev: threading.Event | None = None):
        stop_ev = stop_ev or self._stop
        while not stop_ev.is_set() and self.enabled:
            try:
                self.tick()
            except Exception as e:           # 单轮异常不杀回路，记录后继续
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)
            stop_ev.wait(max(1.0, float(self.cfg["poll_s"])))

    # ── 单轮 ───────────────────────────────────────────────────────────────
    def tick(self):
        f = self._fns
        if not f:
            return
        # Аудит 28.09: раньше весь скан (GMGN/LLM/RPC, 20-40с) шёл под замком сессии, и
        # real-time watcher со стопами ждал его → стоп −25% исполнялся на −50…−90%.
        # Теперь: скан без замка (screen_once сам берёт замок на мутацию позиций),
        # входы — без замка (buy_fn пишет позицию под замком), выходы — под замком (быстро).
        screened = f["screen"](self.chain)
        self._act(screened, f)
        self.stats["ticks"] += 1
        self.stats["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def _act(self, screened: dict, f: dict):
        risk_cfg = f["risk_cfg"]
        if callable(risk_cfg):              # per-user риск: читаем на каждом тике (правки на лету)
            risk_cfg = risk_cfg()
        decisions = screened.get("decisions", [])
        pos_views = {pv.get("address"): pv for pv in screened.get("positions", [])}

        # 1) 入场（熔断/当日上限期间只许平仓，跳过开仓）
        entries = []
        with f["lock"]:
            if not f["halted"]():
                positions = f["positions"]()
                held = {p["address"] for p in positions}
                entries = select_entries(
                    decisions, held, len(positions),
                    (f["max_positions"]() if f.get("max_positions")
                     else risk_cfg.get("max_concurrent_positions", 3)),
                    self.cfg["max_new_per_tick"],
                    require_trigger=self.cfg["require_abc_trigger"],
                    min_priority=self.cfg.get("min_priority", 0),
                    min_buy_ratio=self.cfg.get("min_buy_ratio", 0),
                    max_dump_pct=self.cfg.get("max_dump_pct", 0),
                    max_sm_confluence=self.cfg.get("max_sm_confluence", 0))
        if entries:                                # сеть (pre-entry проверки, цена) — без замка
            for addr, size, _sym in entries:
                try:
                    f["buy"](self.chain, addr, size)
                    self.stats["buys"] += 1
                except Exception as e:        # 组合风控硬拦(409) 等：计 blocked，不算错误
                    self.stats["blocked"] += 1
                    self.stats["last_error"] = str(e)

        # 2) 离场（始终执行，含熔断期）— под замком: те же позиции трогает watcher
        with f["lock"]:
            self._exits(f, pos_views, risk_cfg)

    def _exits(self, f: dict, pos_views: dict, risk_cfg: dict):
        for p in list(f["positions"]()):
            if p.get("chain", "sol") != self.chain:
                continue
            if p.get("self_custody") and not p.get("session"):
                continue                    # Phantom-позиция: продать может только владелец подписью;
                                            # do_sell закрыл бы её «бумажно», а токены остались бы в кошельке
            pnl = float(p.get("pnl", 0.0))
            p["peak_pnl"] = max(float(p.get("peak_pnl", pnl)), pnl)
            p.setdefault("tp_taken", [])
            pv = pos_views.get(p["address"], {})
            ed = decide_exit(p, pv.get("severity", 0), risk_cfg, self.cfg)
            if ed.action != "SELL":
                continue
            partial = ed.rung >= 0 and ed.fraction < 1.0
            if partial:
                p["tp_taken"].append(ed.rung)   # ДО продажи: do_sell сохраняет позиции на диск —
                                                # иначе рестарт между ними = та же ступень дважды
            try:
                res = f["sell"](p["address"], fraction=ed.fraction, reason=ed.reason)
            except Exception as e:
                if partial:
                    p["tp_taken"].remove(ed.rung)
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)
                continue
            if isinstance(res, dict) and res.get("proposed"):
                if partial:                     # N1: только предложение — ступень не взята
                    p["tp_taken"].remove(ed.rung)
                continue
            self.stats["partials" if partial else "sells"] += 1

    # ── 自省 ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return dict(enabled=self.enabled, chain=self.chain,
                    interval_s=self.cfg["poll_s"], cfg=dict(self.cfg),
                    stats=dict(self.stats))


def describe() -> dict:
    """给 /api/bot 用的机器人说明（前端可直接渲染）。"""
    return dict(
        cfg=dict(CFG),
        thesis=("Autonomous execution loop: auto-opens positions only for candidates that pass all gates + ABC Alpha v1 trigger; "
                "exits by hard-stop > escape > trailing take-profit > TP-ladder partials; only closing is allowed during a circuit-break / daily-cap. "
                "Off by default, paper-first; under SHADOW it accumulates SELL records to feed backtest for real win-rate/R before going live."))
