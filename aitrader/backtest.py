"""纸面/SHADOW 回测复盘 —— 从 trade_decisions.jsonl 算已实现 PnL / 胜率 / R 期望。

定位：策略反馈飞轮的"度量"端。我们不另起数据管线，直接复盘日志里真实发生过的决策与平仓：
  • funnel：筛选漏斗（各 gate 拒了多少、最终几个进 ACTION/BUY），看门槛是否过松/过紧。
  • realized：从 SELL 记录算【已实现】胜率、平均 PnL%、平均 R、期望 R、累计 SOL —— 唯一可信的真账。
  • paper：对 SCREEN 候选用 ABC Alpha v1 的信号分估【预期】R（非实盘，标注清楚，仅供上线前判断）。

R 的定义：1R = 硬止损幅度（hard_stop_pct）。pnl% / hard_stop_pct = 该笔的 R 倍数。
⚠️ realized 才是真账；paper 是"如果按这套打法、按 score 当胜率代理"的事前估计，不能当业绩。
"""
from __future__ import annotations

import gzip
import json
import pathlib
import re

import strategy

HERE = pathlib.Path(__file__).resolve().parent
LOG_PATH = HERE / "outputs" / "trade_decisions.jsonl"
ARCHIVE_DIRNAME = "archive"      # <журнал>.parent/archive/<stem>-YYYY-MM.jsonl.gz (ротация в app.log)

_PNL_RE = re.compile(r"PnL\s*([+-]?\d+(?:\.\d+)?)\s*%")


def load_records(path: pathlib.Path | str | None = None) -> list[dict]:
    p = pathlib.Path(path) if path else LOG_PATH
    if not p.exists():
        return []
    out = []
    with p.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


# ── потоковый разбор журнала (память O(1) от размера файла) ────────────────
# Журнал растёт безостановочно (FILTER/SCREEN каждые ~20с 24/7); load_records
# на сотнях МБ раздувает процесс в гигабайты (dict Python ≈ 10-20× JSON-текста).
# Здесь один проход по файлу собирает МАЛЕНЬКУЮ выжимку: счётчики + SELL-записи
# (реальные сделки, их мало) + feature-снапшоты SCREEN. Выжимка кэшируется по
# (size, mtime) — архивы неизменяемы, пересканируется только выросший hot-файл.

def _open_journal(p: pathlib.Path):
    return gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz" else p.open("r")


def _scan_file(p: pathlib.Path) -> dict:
    ext = dict(records=0, actions={}, gates={}, sells=[], feats=[])
    try:
        fh = _open_journal(p)
    except OSError:
        return ext
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ext["records"] += 1
            a = r.get("action", "?")
            ext["actions"][a] = ext["actions"].get(a, 0) + 1
            if a == "REJECT":
                g = str(r.get("gate", "?"))
                ext["gates"][g] = ext["gates"].get(g, 0) + 1
            if a == "SELL":
                ext["sells"].append(r)
            f = _features_of(r)
            if f:
                ext["feats"].append(f)
    return ext


_SCAN_CACHE: dict[str, tuple[tuple, dict]] = {}   # str(path) -> ((size, mtime_ns), выжимка)


def _scan_cached(p: pathlib.Path) -> dict | None:
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    key = str(p)
    hit = _SCAN_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    ext = _scan_file(p)
    if len(_SCAN_CACHE) > 64:
        _SCAN_CACHE.pop(next(iter(_SCAN_CACHE)))
    _SCAN_CACHE[key] = (sig, ext)
    return ext


def journal_files(path: pathlib.Path | str | None = None) -> list[pathlib.Path]:
    """Все сегменты журнала: месячные gzip-архивы (по возрастанию) + hot-файл."""
    p = pathlib.Path(path) if path else LOG_PATH
    files: list[pathlib.Path] = []
    ad = p.parent / ARCHIVE_DIRNAME
    if ad.is_dir():
        files += sorted(ad.glob(f"{p.stem}-*.jsonl.gz"))
    files.append(p)
    return files


def collect(path: pathlib.Path | str | None = None) -> dict:
    """Слитая выжимка hot-файла + всех архивов: полная история для статистики
    без загрузки самого журнала в память."""
    merged = dict(records=0, actions={}, gates={}, sells=[], feats=[])
    for f in journal_files(path):
        if not f.exists():
            continue
        ext = _scan_cached(f)
        if not ext:
            continue
        merged["records"] += ext["records"]
        for k, v in ext["actions"].items():
            merged["actions"][k] = merged["actions"].get(k, 0) + v
        for k, v in ext["gates"].items():
            merged["gates"][k] = merged["gates"].get(k, 0) + v
        merged["sells"] += ext["sells"]
        merged["feats"] += ext["feats"]
    return merged


def sell_records(path: pathlib.Path | str | None = None) -> list[dict]:
    """Все SELL-записи (hot + архивы) — ground truth для PnL/review. Их мало
    (реальные сделки), в отличие от FILTER/SCREEN-шума."""
    return list(collect(path)["sells"])


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


def _is_full(r: dict) -> bool:
    frac = r.get("fraction")
    try:
        return frac is None or float(frac) >= 0.999
    except (TypeError, ValueError):
        return True


def positions_from_sells(records: list[dict]) -> list[dict]:
    """SELL-записи → ЗАКРЫТЫЕ ПОЗИЦИИ (частичные TP + финальное закрытие = одна сделка).

    Аудит 28.09: раньше «сделкой» считалась каждая SELL-запись. Частичные TP — всегда
    выигрыши на малых долях, стоп — проигрыш на остатке → винрейт/avg_R/expectancy
    выходили ПОЛОЖИТЕЛЬНЫМИ при отрицательном SOL; а «только полные закрытия» теряли
    прибыль TP (−10.4 вместо реальных −3.5 SOL). Здесь: pnl позиции = ΣSOL / Σразмера
    её продаж (взвешено по размеру), размер = исходный. Ещё открытые хвосты не входят."""
    open_: dict[tuple, dict] = {}
    out: list[dict] = []
    for r in records:
        if r.get("action") != "SELL":
            continue
        p = _pnl_pct(r)
        if p is None:
            continue
        size = r.get("size_sol")
        size = float(size) if isinstance(size, (int, float)) else 0.0
        key = (r.get("pubkey") or "local", r.get("address") or r.get("symbol") or "?")
        acc = open_.setdefault(key, dict(sol=0.0, size=0.0, attrib=None, legs=0))
        acc["sol"] += p * size
        acc["size"] += size
        acc["legs"] += 1
        if r.get("attrib"):
            acc["attrib"] = r.get("attrib")
        if _is_full(r):
            open_.pop(key, None)
            pnl = (acc["sol"] / acc["size"]) if acc["size"] > 0 else p
            out.append(dict(ts=r.get("ts", ""), symbol=r.get("symbol", "?"), address=key[1],
                            pubkey=key[0], mode=r.get("mode"), pnl=pnl,
                            size_sol=round(acc["size"], 6), sol=round(acc["sol"], 6),
                            attrib=acc["attrib"] or {}, hold_min=r.get("hold_min"),
                            legs=acc["legs"], reason=r.get("reason", "")))
    return out


def realized(records: list[dict], hard_stop_pct: float | None = None) -> dict:
    """Реализованный результат. total_pnl_sol — cash-flow ВСЕХ продаж (= календарь, реальные
    деньги); winrate/R/expectancy — по закрытым ПОЗИЦИЯМ (см. positions_from_sells)."""
    hs = hard_stop_pct if hard_stop_pct else strategy.PRESET["hard_stop_pct"]
    total_sol, legs = 0.0, 0
    for r in records:
        if r.get("action") != "SELL":
            continue
        p = _pnl_pct(r)
        if p is None:
            continue
        legs += 1
        size = r.get("size_sol")
        if isinstance(size, (int, float)):
            total_sol += p * float(size)
    pos = positions_from_sells(records)
    pnls = [t["pnl"] for t in pos]
    n = len(pnls)
    if n == 0:
        return dict(trades=0, note="No closed trades to review (run SELL under SHADOW first, then check real results)")
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
        trades=n, sell_records=legs, win_rate=round(win_rate, 3),
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
    return _paper_feats([f for f in (_features_of(r) for r in records) if f], trig)


def _paper_feats(feats: list[dict], trig: dict | None = None) -> dict:
    if not feats:
        return dict(candidates=0, note="No feature snapshots in log (run screen_once first, then review)")
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


def summary(path: pathlib.Path | str | None = None, trig: dict | None = None,
            hard_stop_pct: float | None = None) -> dict:
    # Потоково (hot + архивы): не материализуем журнал списком dict'ов — на
    # многомесячном журнале это гигабайты RSS при каждом вызове эндпоинта.
    c = collect(path)
    return dict(
        strategy=strategy.NAME, version=strategy.VERSION,
        records=c["records"],
        funnel=dict(by_action=c["actions"], rejects_by_gate=c["gates"], total=c["records"]),
        realized=realized(c["sells"], hard_stop_pct),
        paper=dict(_paper_feats(c["feats"], trig),
                   note="model estimate from entry score, NOT realized results; "
                        "cannot go negative by construction — do not use for go/no-go"),
    )


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
