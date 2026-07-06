"""ABC Alpha v1 —— 具名策略预设 + 入场信号评估（post-grad momentum + smart-money 共识）。

定位（务必看清）：这是【信号/择时】层，不是复制跟单。撮合仍走我们自己的快速通道。
本模块只做两件事：
  1) PRESET：把"ABC Alpha v1"这套打法的参数固化成一个具名预设，可一键并入 app.CFG / filters。
  2) evaluate(f)：在 hard_gates（避雷）之后，对幸存者再判一道【alpha 触发条件】，
     返回结构化信号（pass/score/预期 R/理由），供排序加权与回测复盘共用。

打法（一句话）：不抢 slot-0，盯【毕业后 0–15 分钟】窗口；当 避雷过关 + 至少 2 只
聪明钱在场（我方私有跟踪钱包优先，其次 GMGN smart_degen/KOL 共识）+ 动能向上（5m 涨、
买盘占优）+ 流动性达标 时触发；退出走 TP 阶梯 + 移动止盈 + 硬止损 + 逃生预警；
纪律靠 连亏熔断 + 当日亏损上限 + 实盘前先在 trade_decisions.jsonl 上做纸面验证。
"""
from __future__ import annotations

from dataclasses import dataclass, field

NAME = "ABC Alpha v1"
VERSION = "1.0"

# 触发阈值（独立于 CFG 的避雷门槛；这里是"选优"而非"避雷"）。
TRIGGER = {
    "min_tracked_or_smart": 2,   # 至少 2 只聪明钱在场（tracked_hits 或 sm_confluence 任一达标）
    "min_chg_5m": 0.0,           # 5 分钟动能须为正（在涨）
    "min_buy_ratio": 0.55,       # 买盘占比下限（买方主导）
    "min_liquidity": 8000.0,     # 最小流动性（USD），防"进得去出不来"
    "min_age_min": 1.0,          # 不抢 slot-0：至少 1 分钟（避开狙击横行的极新盘）
    "max_age_min": 45.0,         # 动能窗口：超过则视为已过最佳追入点（毕业后偏好 0–15min，45 为硬上限）
    "ideal_age_max": 15.0,       # 理想窗口上限（0–15min 给满分，之后线性衰减）
}

# 可一键并入 app.CFG 的参数（实盘前的"纪律收紧"版本：并发与风险都收窄）。
# 注意：默认不自动应用，由 /api/strategy/apply 或测试显式调用 apply_to_cfg。
PRESET = {
    "rank_profile": "abc_alpha_v1",
    "min_smart_money_confluence": 1,     # 避雷层仍只要 1（"≥2"是 alpha 触发层的事，避免误杀）
    "buy_ratio_pass": 0.55,
    "buy_ratio_reject": 0.45,
    "momentum_reject_chg1h": -0.12,
    "tp_ladder": [(0.60, 0.40), (1.50, 0.30), (3.00, 0.20)],  # 三段：落袋 + 留底搏大
    "trailing_pct": 0.25,
    "hard_stop_pct": 0.35,
    "max_concurrent_positions": 3,       # 纪律：实盘并发收回 3
    "risk_per_trade": 0.01,
    "kill_switch_consec_losses": 3,
}

# 并入 filters 的部分（与 DEFAULT_FILTERS 同键，UI/落盘共用一套）。
PRESET_FILTERS = {
    "min_liquidity": TRIGGER["min_liquidity"],
    "min_age_min": TRIGGER["min_age_min"],
    "max_age_min": TRIGGER["max_age_min"],
    "require_renounced_freeze": True,
}


@dataclass
class ABCSignal:
    triggered: bool
    score: float                 # 0..100，alpha 强度（与 priority_score 不同维度，专用于本策略复盘）
    r_expect: float              # 预期 R 倍数（基于 TP 阶梯 + 命中概率粗估）
    reasons: list = field(default_factory=list)   # 命中/未命中的人读理由

    def as_dict(self) -> dict:
        return dict(strategy=NAME, version=VERSION, triggered=self.triggered,
                    score=round(self.score, 1), r_expect=round(self.r_expect, 2),
                    reasons=self.reasons)


def _get(f, name, default=0.0):
    """兼容 TokenFeatures 对象与 _feat() dict 两种入参。"""
    if isinstance(f, dict):
        return f.get(name, default)
    return getattr(f, name, default)


def evaluate(f, trig: dict | None = None, preset: dict | None = None) -> ABCSignal:
    """对一个已过避雷门槛的候选评入场信号（默认按 ABC Alpha v1 阈值）。

    入参 f 可以是 TokenFeatures 或 _feat() 的 dict。返回 ABCSignal。
    评分逻辑全可解释（无黑盒）：四个维度命中即累计，差一项则降级 watch。
    trig/preset 可传其他策略的阈值/预设（见 STRATEGIES），r_expect 用该预设的阶梯/止损。
    """
    t = trig or TRIGGER
    p = preset or PRESET
    smart = max(int(_get(f, "tracked_hits", 0)), int(_get(f, "sm_confluence", 0)))
    chg5 = float(_get(f, "chg_5m", 0.0))
    buyr = float(_get(f, "buy_ratio", 0.5))
    liq = float(_get(f, "liquidity", 0.0))
    age = float(_get(f, "age_min", 0.0))

    reasons, score = [], 0.0

    ok_smart = smart >= t["min_tracked_or_smart"]
    reasons.append(f"{'✓' if ok_smart else '✗'} Smart money present {smart} (need ≥{t['min_tracked_or_smart']})")
    if ok_smart:
        score += min(35, 18 + (smart - t["min_tracked_or_smart"]) * 8)

    ok_mom = chg5 >= t["min_chg_5m"]
    reasons.append(f"{'✓' if ok_mom else '✗'} 5m momentum {chg5:+.1%} (need ≥{t['min_chg_5m']:+.0%})")
    if ok_mom:
        score += min(25, 12 + chg5 * 120)

    ok_buy = buyr >= t["min_buy_ratio"]
    reasons.append(f"{'✓' if ok_buy else '✗'} Buy ratio {buyr:.0%} (need ≥{t['min_buy_ratio']:.0%})")
    if ok_buy:
        score += min(20, (buyr - t["min_buy_ratio"]) * 100 + 10)

    ok_liq = liq >= t["min_liquidity"]
    reasons.append(f"{'✓' if ok_liq else '✗'} Liquidity ${liq:,.0f} (need ≥${t['min_liquidity']:,.0f})")
    if ok_liq:
        score += 10

    # 币龄窗口：理想 0–15min 给满分，到 max_age 线性衰减为 0；过老/过新都扣。
    ok_age = t["min_age_min"] <= age <= t["max_age_min"]
    reasons.append(f"{'✓' if ok_age else '✗'} Token age {age:.0f}min (window {t['min_age_min']:.0f}–{t['max_age_min']:.0f})")
    if ok_age:
        if age <= t["ideal_age_max"]:
            score += 10
        else:
            span = max(1e-9, t["max_age_min"] - t["ideal_age_max"])
            score += 10 * max(0.0, (t["max_age_min"] - age) / span)

    score = max(0.0, min(100.0, score))
    # 触发要求：聪明钱 + 动能 + 买盘 三个核心条件全中（流动性/币龄为加分但也须达标的硬窗口）。
    triggered = ok_smart and ok_mom and ok_buy and ok_liq and ok_age

    # 预期 R：以 TP 阶梯加权命中收益、硬止损为 1R 下行，用 score 调命中概率粗估。
    r_expect = _expected_r(score, p["tp_ladder"], p["hard_stop_pct"]) if triggered else 0.0
    return ABCSignal(triggered=triggered, score=score, r_expect=r_expect, reasons=reasons)


def _expected_r(score: float, tp_ladder, hard_stop_pct: float) -> float:
    """用 score(0..100) 当胜率代理，TP 阶梯当上行、硬止损当下行，估单笔期望 R。
    这是【粗估】，仅供排序与纸面预期；真实 R 必须由 backtest 从已实现 PnL 算。"""
    p_win = 0.25 + 0.40 * (score / 100.0)        # 25%~65% 命中概率代理
    up = sum(gain * sell for gain, sell in tp_ladder)   # 加权上行（以入场为 1）
    up_r = up / hard_stop_pct                     # 换算成 R（1R = 硬止损幅度）
    return p_win * up_r - (1 - p_win) * 1.0


def apply_to_cfg(cfg: dict, filters: dict | None = None) -> dict:
    """把 PRESET 并入给定 CFG（就地更新并返回）；可选同时并入 filters。
    用户决策：实盘前显式调用，不在 import 时自动改全局，避免静默改变行为。"""
    cfg.update(PRESET)
    if filters is not None:
        filters.update(PRESET_FILTERS)
    return cfg


def describe() -> dict:
    """给 /api/strategy 用的策略说明（前端可直接渲染）。"""
    return dict(name=NAME, version=VERSION, trigger=TRIGGER, preset=PRESET,
                preset_filters=PRESET_FILTERS,
                thesis=("Don't fight for slot-0; watch the 0–15min window after graduation; anti-rug passed + ≥2 smart money present + "
                        "5m momentum up + buying dominant + liquidity threshold met → trigger; "
                        "exit via TP-ladder / trailing take-profit / hard-stop / escape alert; consecutive-loss circuit-break + daily cap + paper-first."))


# ──────────────────────────────────────────────────────────────────────────
# 策略注册表：具名预设可选（每用户各选各的，见 app.UserSession.strategy_id）。
# 结构同构：trigger（入场触发阈值）+ preset（并入 CFG 的纪律参数）+ preset_filters。
# ──────────────────────────────────────────────────────────────────────────
def _mk(sid, name, version, thesis, trigger, preset, preset_filters):
    return dict(id=sid, name=name, version=version, thesis=thesis,
                trigger=trigger, preset=preset, preset_filters=preset_filters)

_SNIPER_TRIGGER = dict(TRIGGER, min_tracked_or_smart=3, min_buy_ratio=0.60,
                       min_liquidity=15000.0, max_age_min=30.0, ideal_age_max=10.0)
_SNIPER_PRESET = dict(PRESET, max_concurrent_positions=2, risk_per_trade=0.005,
                      hard_stop_pct=0.25, trailing_pct=0.20,
                      tp_ladder=[(0.40, 0.50), (1.00, 0.30), (2.00, 0.20)],
                      kill_switch_consec_losses=2)
_DEGEN_TRIGGER = dict(TRIGGER, min_tracked_or_smart=1, min_buy_ratio=0.52,
                      min_liquidity=5000.0, max_age_min=90.0, ideal_age_max=25.0)
_DEGEN_PRESET = dict(PRESET, max_concurrent_positions=5, risk_per_trade=0.015,
                     hard_stop_pct=0.45, trailing_pct=0.35,
                     tp_ladder=[(1.00, 0.30), (3.00, 0.30), (8.00, 0.20)],
                     kill_switch_consec_losses=4)

DEFAULT_STRATEGY = "abc_alpha_v1"
STRATEGIES = {
    "abc_alpha_v1": _mk(
        "abc_alpha_v1", NAME, VERSION,
        ("Сбалансированная: пост-градация 0–15 мин, ≥2 умных кошелька, моментум 5m + "
         "перевес покупок; TP-лестница, трейлинг, жёсткий стоп."),
        TRIGGER, PRESET, PRESET_FILTERS),
    "abc_sniper_v1": _mk(
        "abc_sniper_v1", "ABC Sniper (консервативная)", "1.0",
        ("Мало сделок, высокая планка: ≥3 умных кошелька, покупки ≥60%, ликвидность ≥$15k, "
         "окно до 30 мин; риск/сделку вдвое ниже, стоп короче, фьюз после 2 убытков."),
        _SNIPER_TRIGGER, _SNIPER_PRESET,
        dict(min_liquidity=_SNIPER_TRIGGER["min_liquidity"], min_age_min=1.0,
             max_age_min=_SNIPER_TRIGGER["max_age_min"], require_renounced_freeze=True)),
    "abc_degen_v1": _mk(
        "abc_degen_v1", "ABC Degen (агрессивная)", "1.0",
        ("Больше входов, шире окно (до 90 мин), достаточно 1 умного кошелька; "
         "стоп дальше, цели выше (до 8x), риск/сделку выше — только для бумажной обкатки."),
        _DEGEN_TRIGGER, _DEGEN_PRESET,
        dict(min_liquidity=_DEGEN_TRIGGER["min_liquidity"], min_age_min=1.0,
             max_age_min=_DEGEN_TRIGGER["max_age_min"], require_renounced_freeze=True)),
}

def get(sid: str) -> dict:
    """取具名策略（未知 id → 默认 ABC Alpha v1，绝不 KeyError）。"""
    return STRATEGIES.get(sid) or STRATEGIES[DEFAULT_STRATEGY]

def evaluate_for(sid: str, f) -> ABCSignal:
    st = get(sid)
    return evaluate(f, trig=st["trigger"], preset=st["preset"])

def describe_all(active: str = DEFAULT_STRATEGY) -> dict:
    return dict(active=get(active)["id"],
                strategies=[STRATEGIES[k] for k in STRATEGIES])
