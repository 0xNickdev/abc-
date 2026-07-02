"""纸面/SHADOW 回测复盘 —— 从 trade_decisions.jsonl 算已实现 PnL / 胜率 / R 期望。

定位：策略反馈飞轮的"度量"端。我们不另起数据管线，直接复盘日志里真实发生过的决策与平仓：
  • funnel：筛选漏斗（各 gate 拒了多少、最终几个进 ACTION/BUY），看门槛是否过松/过紧。
  • realized：从 SELL 记录算【已实现】胜率、平均 PnL%、平均 R、期望 R、累计 SOL —— 唯一可信的真账。
  • paper：对 SCREEN 候选用 ABC Alpha v1 的信号分估【预期】R（非实盘，标注清楚，仅供上线前判断）。

R 的定义：1R = 硬止损幅度（hard_stop_pct）。pnl% / hard_stop_pct = 该笔的 R 倍数。
⚠️ realized 才是真账；paper 是"如果按这套打法、按 score 当胜率代理"的事前估计，不能当业绩。
"""
from __future__ import annotations

import json
import pathlib
import re

import strategy

HERE = pathlib.Path(__file__).resolve().parent
LOG_PATH = HERE / "outputs" / "trade_decisions.jsonl"

_PNL_RE = re.compile(r"PnL\s*([+-]?\d+(?:\.\d+)?)\s*%")


def load_records(path: pathlib.Path | str | None = None) -> list[dict]:
    p = pathlib.Path(path) if path else LOG_PATH
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _pnl_pct(rec: dict):
    """优先取 extra.pnl（小数，0.12=+12%），否则从 reason 文本里解析 'PnL +12.0%'。"""
    if isinstance(rec.get("pnl"), (int, float)):
        return float(rec["pnl"])
    m = _PNL_RE.search(rec.get("reason", "") or "")
    if m:
        return float(m.group(1)) / 100.0
    return None


def funnel(records: list[dict]) -> dict:
    """筛选漏斗统计：按 action 计数 + 按拒绝 gate 归类。"""
    by_action: dict[str, int] = {}
    rejects_by_gate: dict[str, int] = {}
    for r in records:
        a = r.get("action", "?")
        by_action[a] = by_action.get(a, 0) + 1
        if a == "REJECT":
            g = str(r.get("gate", "?"))
            rejects_by_gate[g] = rejects_by_gate.get(g, 0) + 1
    return dict(by_action=by_action, rejects_by_gate=rejects_by_gate,
                total=len(records))


def realized(records: list[dict], hard_stop_pct: float | None = None) -> dict:
    """从 SELL 记录算已实现业绩。无平仓记录则返回 n=0（诚实留空，不编造）。"""
    hs = hard_stop_pct if hard_stop_pct else strategy.PRESET["hard_stop_pct"]
    sells = [r for r in records if r.get("action") == "SELL"]
    pnls, total_sol = [], 0.0
    for r in sells:
        p = _pnl_pct(r)
        if p is None:
            continue
        pnls.append(p)
        size = r.get("size_sol")
        if isinstance(size, (int, float)):
            total_sol += p * float(size)
    n = len(pnls)
    if n == 0:
        return dict(trades=0, note="无平仓记录可复盘（先在 SHADOW 跑出 SELL 再看真账）")
    wins = [x for x in pnls if x > 0]
    rs = [x / hs for x in pnls]
    avg_pnl = sum(pnls) / n
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    losses = [x for x in pnls if x <= 0]
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    # 期望 R = 胜率*平均盈利R - 败率*平均亏损R（亏损取绝对值）
    expectancy_r = win_rate * (avg_win / hs) - (1 - win_rate) * (abs(avg_loss) / hs)
    return dict(
        trades=n, win_rate=round(win_rate, 3),
        avg_pnl_pct=round(avg_pnl, 4), avg_R=round(sum(rs) / n, 3),
        avg_win_pct=round(avg_win, 4), avg_loss_pct=round(avg_loss, 4),
        expectancy_R=round(expectancy_r, 3),
        total_pnl_sol=round(total_sol, 4),
        best_pct=round(max(pnls), 4), worst_pct=round(min(pnls), 4),
    )


def _features_of(rec: dict) -> dict | None:
    """从 SCREEN/ACTION 决策记录里取出特征 dict（screen_once 落的是 decision.features）。"""
    d = rec.get("decision")
    if isinstance(d, dict) and isinstance(d.get("features"), dict):
        return d["features"]
    if isinstance(rec.get("features"), dict):
        return rec["features"]
    return None


def paper(records: list[dict], trig: dict | None = None) -> dict:
    """对带特征快照的候选跑 ABC Alpha v1 评估，给出【事前】触发率与平均预期 R。
    用于上线前判断"这套打法在历史候选上会不会频繁触发、预期 R 是否为正"。非实盘业绩。"""
    feats = [f for f in (_features_of(r) for r in records) if f]
    if not feats:
        return dict(candidates=0, note="日志中无特征快照（运行 screen_once 后再复盘）")
    sigs = [strategy.evaluate(f, trig) for f in feats]
    trig_sigs = [s for s in sigs if s.triggered]
    n = len(sigs)
    rexp = [s.r_expect for s in trig_sigs]
    return dict(
        candidates=n,
        triggered=len(trig_sigs),
        trigger_rate=round(len(trig_sigs) / n, 3) if n else 0.0,
        avg_score=round(sum(s.score for s in sigs) / n, 1) if n else 0.0,
        avg_r_expect_triggered=round(sum(rexp) / len(rexp), 2) if rexp else 0.0,
    )


def summary(path: pathlib.Path | str | None = None, trig: dict | None = None) -> dict:
    recs = load_records(path)
    return dict(
        strategy=strategy.NAME, version=strategy.VERSION,
        records=len(recs),
        funnel=funnel(recs),
        realized=realized(recs),
        paper=paper(recs, trig),
    )


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
