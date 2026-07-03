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

import threading
import time
from dataclasses import dataclass

# 机器人自身的执行参数（与 strategy.PRESET / app.CFG 的风控参数互补，不重复其值）。
CFG = {
    "mode": "n2",                 # n1=предлагает, человек подтверждает · n2=исполняет сам (paper/LIVE-замки)
    "max_new_per_tick": 2,        # 每轮最多新开仓数（防一轮梭哈）
    "poll_s": 20.0,               # 自主轮询间隔（秒）
    "escape_severity_exit": 60,   # 逃生严重度 ≥ 此值即清仓离场（比人工 escape_severity 略激进）
    "trail_activate_pct": 0.30,   # 浮盈达此幅度后才启用移动止盈（避免刚建仓就被噪声扫出）
    "require_abc_trigger": True,  # 只对 ABC Alpha v1 触发的候选自动建仓
}


@dataclass
class ExitDecision:
    action: str          # "SELL" | "HOLD"
    fraction: float      # 清仓比例 0..1（TP 阶梯为分批，其余为 1.0 全清）
    reason: str
    rung: int = -1       # 命中的 TP 阶梯档位下标（仅分批离场用，供调用方标记已兑现）


def select_entries(decisions, held_addrs, n_open, max_concurrent, max_new,
                   *, require_trigger: bool = True):
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

    if pnl <= -hard_stop:
        return ExitDecision("SELL", 1.0, f"硬止损 PnL {pnl:+.0%} ≤ -{hard_stop:.0%}")
    if severity is not None and float(severity) >= bc["escape_severity_exit"]:
        return ExitDecision("SELL", 1.0, f"逃生离场 严重度 {severity}≥{bc['escape_severity_exit']}")
    if peak >= bc["trail_activate_pct"] and (peak - pnl) >= trail:
        return ExitDecision("SELL", 1.0, f"移动止盈 自峰值 {peak:+.0%} 回撤 {peak - pnl:.0%}≥{trail:.0%}")
    # TP 阶梯：命中尚未兑现的【最低未取】档，分批落袋（一轮只取一档，避免一次扫光）
    for i, rung in enumerate(ladder):
        if i in taken:
            continue
        gain, frac = rung
        if pnl >= float(gain):
            return ExitDecision("SELL", float(frac), f"TP{i + 1} 命中 +{float(gain):.0%}，落袋 {float(frac):.0%}", rung=i)
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
              risk_cfg, lock, halted_fn=None) -> bool:
        """注入 app 的能力并启动。已在跑则返回 False（幂等）。"""
        if self.enabled:
            return False
        self.chain = chain
        self._fns = dict(screen=screen_fn, buy=buy_fn, sell=sell_fn,
                         positions=positions_fn, risk_cfg=risk_cfg, lock=lock,
                         halted=halted_fn or (lambda: False))
        self.enabled = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        self.enabled = False
        self._stop.set()
        return True

    def _loop(self):
        while not self._stop.is_set() and self.enabled:
            try:
                self.tick()
            except Exception as e:           # 单轮异常不杀回路，记录后继续
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)
            self._stop.wait(self.cfg["poll_s"])

    # ── 单轮 ───────────────────────────────────────────────────────────────
    def tick(self):
        f = self._fns
        if not f:
            return
        with f["lock"]:
            screened = f["screen"](self.chain)
            self._act(screened, f)
        self.stats["ticks"] += 1
        self.stats["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def _act(self, screened: dict, f: dict):
        positions = f["positions"]()
        risk_cfg = f["risk_cfg"]
        decisions = screened.get("decisions", [])
        pos_views = {pv.get("address"): pv for pv in screened.get("positions", [])}

        # 1) 入场（熔断/当日上限期间只许平仓，跳过开仓）
        if not f["halted"]():
            held = {p["address"] for p in positions}
            entries = select_entries(
                decisions, held, len(positions),
                risk_cfg.get("max_concurrent_positions", 3),
                self.cfg["max_new_per_tick"],
                require_trigger=self.cfg["require_abc_trigger"])
            for addr, size, _sym in entries:
                try:
                    f["buy"](self.chain, addr, size)
                    self.stats["buys"] += 1
                except Exception as e:        # 组合风控硬拦(409) 等：计 blocked，不算错误
                    self.stats["blocked"] += 1
                    self.stats["last_error"] = str(e)

        # 2) 离场（始终执行，含熔断期）
        for p in list(positions):
            if p.get("chain", "sol") != self.chain:
                continue
            pnl = float(p.get("pnl", 0.0))
            p["peak_pnl"] = max(float(p.get("peak_pnl", pnl)), pnl)
            p.setdefault("tp_taken", [])
            pv = pos_views.get(p["address"], {})
            ed = decide_exit(p, pv.get("severity", 0), risk_cfg, self.cfg)
            if ed.action != "SELL":
                continue
            try:
                f["sell"](p["address"], fraction=ed.fraction, reason=ed.reason)
                if ed.rung >= 0 and ed.fraction < 1.0:
                    p["tp_taken"].append(ed.rung)
                    self.stats["partials"] += 1
                else:
                    self.stats["sells"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)

    # ── 自省 ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return dict(enabled=self.enabled, chain=self.chain,
                    interval_s=self.cfg["poll_s"], cfg=dict(self.cfg),
                    stats=dict(self.stats))


def describe() -> dict:
    """给 /api/bot 用的机器人说明（前端可直接渲染）。"""
    return dict(
        cfg=dict(CFG),
        thesis=("自主执行回路：只对【通过全部闸门 + ABC Alpha v1 触发】的候选自动建仓；"
                "离场按 硬止损>逃生>移动止盈>TP阶梯分批；熔断/当日上限期间只许平仓。"
                "默认关闭、纸面优先，SHADOW 下攒 SELL 记录喂 backtest 出真胜率/R 后再考虑上线。"))
