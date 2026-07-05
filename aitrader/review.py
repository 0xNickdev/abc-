"""Ночной review-loop —— офлайн-механизм обучения на своих данных.

Философия (см. переписку): НЕ RL в реальном времени, а офлайн-цикл, который раз в сутки
разбирает сделки дня, взвешивает кошельки/KOL по ДОКАЗАННОМУ эджу и предлагает правки
конфига («ужать этот фильтр», «KOL X ненадёжен — снизить вес», «поднять min_priority»).
Всё — предложения; применение только явным кликом (/api/review/apply). Пока данных мало
(< MIN_TRADES) веса нейтральны (1.0), пороги не трогаем — не учимся на шуме.

Топливо: attrib на SELL-записи журнала (какие tracked-кошельки/KOL были «в монете» на
входе) + реализованный pnl. Это ставит app.py при закрытии позиции. Здесь мы это едим.

Чистый модуль: НЕ импортирует app (иначе цикл). Ест записи журнала (list[dict]) и
переиспользует разбор из backtest. Экспорт наружу — через app (эндпоинты + ночной поток).
"""
from __future__ import annotations

import datetime

import backtest  # load_records / _pnl_pct — единый разбор журнала (не дублируем)
import kol  # rating() — надёжность KOL по пик-мультипликатору

MIN_TRADES = 5        # ниже — эдж кошелька/бакета нейтрален (не тюним на шуме)
MIN_KOL_CALLS = 4     # ниже — KOL не оцениваем
EDGE_GAP_R = 0.3      # разрыв expectancy(R) между «хорошей» и «плохой» стороной для предложения
DEFAULT_HARD_STOP = 0.35


# ── разбор журнала → закрытые сделки с атрибуцией ───────────────────────────
def _is_full_sell(r: dict) -> bool:
    """Полное закрытие (не частичный TP): fraction нет или ≈1.0. Реализованный исход."""
    if r.get("action") != "SELL":
        return False
    frac = r.get("fraction")
    try:
        return frac is None or float(frac) >= 0.999
    except (TypeError, ValueError):
        return True


def closed_trades(records: list[dict]) -> list[dict]:
    """SELL-записи с посчитанным pnl → [{ts,symbol,pnl,size_sol,sol,attrib,hold_min}]."""
    out = []
    for r in records:
        if not _is_full_sell(r):
            continue
        p = backtest._pnl_pct(r)
        if p is None:
            continue
        size = float(r.get("size_sol", 0.0) or 0.0)
        out.append(dict(ts=r.get("ts", ""), symbol=r.get("symbol", "?"), pnl=p,
                        size_sol=size, sol=round(p * size, 6),
                        attrib=r.get("attrib") or {}, hold_min=r.get("hold_min")))
    return out


# ── статистика/веса ─────────────────────────────────────────────────────────
def _stats(pnls: list[float], hs: float) -> dict:
    """Реализованные winrate / средний pnl / ожидание в R по списку долей pnl."""
    n = len(pnls)
    if n == 0:
        return dict(trades=0, winrate=0.0, avg_pnl=0.0, expectancy_R=0.0)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    wr = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    exp_r = wr * (avg_win / hs) - (1 - wr) * (abs(avg_loss) / hs)
    return dict(trades=n, winrate=round(wr, 3), avg_pnl=round(sum(pnls) / n, 4),
                expectancy_R=round(exp_r, 3))


def _weight(exp_r: float) -> float:
    """Ожидание в R → множитель эджа. 0R → 1.0; +1R → 1.5; -1R → 0.5; насыщение [0.3, 2.0]."""
    return round(max(0.3, min(2.0, 1.0 + 0.5 * exp_r)), 3)


def _confidence(n: int) -> str:
    return "high" if n >= 20 else ("medium" if n >= 10 else "low")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ── эдж кошельков (топливо edge-weighting) ──────────────────────────────────
def wallet_edge(records: list[dict], min_trades: int = MIN_TRADES,
                hard_stop: float = DEFAULT_HARD_STOP) -> dict:
    """Реализованный эдж каждого tracked-кошелька из закрытых сделок, где он был на входе.
    weight нейтрален (1.0), пока сделок < min_trades — не взвешиваем по шуму."""
    by: dict[str, list] = {}
    for t in closed_trades(records):
        for name in (t["attrib"].get("tracked") or []):
            by.setdefault(name, []).append(t["pnl"])
    out = {}
    for name, pnls in by.items():
        st = _stats(pnls, hard_stop)
        enough = st["trades"] >= min_trades
        st["weight"] = _weight(st["expectancy_R"]) if enough else 1.0
        st["sample_ok"] = enough
        out[name] = st
    return out


# ── обзор KOL (пик-мультипликатор из kol.rating) ────────────────────────────
def kol_review(min_calls: int = MIN_KOL_CALLS) -> list[dict]:
    """Надёжность KOL'ов: колл = упоминание CA, win = пик ≥1.5x. Флаг «снизить вес» при
    низком winrate на достаточном числе коллов."""
    try:
        rating = kol.rating()
    except Exception:
        rating = []
    out = []
    for r in rating:
        calls = int(r.get("calls", 0))
        if calls < min_calls:
            continue
        wr = float(r.get("winrate", 0.0))
        out.append(dict(username=r.get("username", "?"), calls=calls, winrate=wr,
                        median_x=r.get("median_x", 0.0),
                        verdict="reliable" if wr >= 0.4 else "unreliable"))
    return out


# ── предложения по конфигу (бакеты фич vs исход) ────────────────────────────
def _split(trades: list[dict], key: str, thr: float) -> tuple[list, list]:
    """Разбить закрытые сделки по фиче attrib[key] относительно порога → (ниже, ≥порога)."""
    lo, hi = [], []
    for t in trades:
        v = t["attrib"].get(key)
        if v is None:
            continue
        (lo if float(v) < float(thr) else hi).append(t)
    return lo, hi


def _bad_side_proposal(bad, good, hs, min_trades, *, target, param,
                       current, suggested, rationale) -> list[dict]:
    """Если «плохая» сторона бакета убыточна (ожидание<0) и заметно хуже «хорошей» —
    предложить правку. Не предлагаем, если уже стоит нужное значение или мало данных."""
    bp = [t["pnl"] for t in bad]
    if len(bp) < min_trades:
        return []
    bs = _stats(bp, hs)
    gs = _stats([t["pnl"] for t in good], hs)
    if bs["expectancy_R"] >= 0 or (gs["expectancy_R"] - bs["expectancy_R"]) < EDGE_GAP_R:
        return []
    if _num(current) is not None and _num(current) == _num(suggested):
        return []
    return [dict(target=target, param=param, current=current, suggested=suggested,
                 rationale=rationale, applicable=target in ("CFG", "filters", "bot"),
                 confidence=_confidence(len(bp)), evidence=dict(bad=bs, good=gs))]


def propose(records: list[dict], cfg: dict | None = None, filters: dict | None = None,
            trigger: dict | None = None, min_trades: int = MIN_TRADES) -> list[dict]:
    """Конкретные правки конфига из реализованных исходов. Пусто, пока сделок < min_trades."""
    cfg = cfg or {}
    filters = filters or {}
    trigger = trigger or {}
    hs = float(cfg.get("hard_stop_pct", DEFAULT_HARD_STOP))
    trades = closed_trades(records)
    if len(trades) < min_trades:
        return []
    props: list[dict] = []

    # 1) поднять min_priority бота, если слабые сетапы (низкий приоритет) убыточны
    lo, hi = _split(trades, "priority", 70)
    props += _bad_side_proposal(
        lo, hi, hs, min_trades, target="bot", param="min_priority",
        current=cfg.get("bot_min_priority", 0), suggested=70,
        rationale="Сетапы с priority<70 проигрывают — поднять планку автопокупки бота до 70.")

    # 2) включить/ужать max_age_min, если старые монеты (вне окна стратегии) хуже
    aw = float(trigger.get("max_age_min", 45) or 45)
    lo, hi = _split(trades, "age_min", aw)      # hi = старше окна
    props += _bad_side_proposal(
        hi, lo, hs, min_trades, target="filters", param="max_age_min",
        current=filters.get("max_age_min", 0.0), suggested=aw,
        rationale=f"Сделки старше {aw:.0f}min в среднем хуже — включить фильтр max_age_min={aw:.0f}.")

    # 3) поднять buy_ratio_reject, если слабый перевес покупок убыточен
    lo, hi = _split(trades, "buy_ratio", 0.50)
    props += _bad_side_proposal(
        lo, hi, hs, min_trades, target="CFG", param="buy_ratio_reject",
        current=cfg.get("buy_ratio_reject", 0.42), suggested=0.50,
        rationale="Сделки с buy_ratio<50% проигрывают — поднять порог отбраковки派发/接盘.")

    # 4) поднять требуемый консенсус, если без умных кошельков исход хуже
    lo, hi = _split(trades, "sm_confluence", 1)  # lo = 0 умных в场
    props += _bad_side_proposal(
        lo, hi, hs, min_trades, target="CFG", param="min_smart_money_confluence",
        current=cfg.get("min_smart_money_confluence", 1), suggested=2,
        rationale="Без умных кошельков в场 исход хуже — поднять требуемый консенсус до 2.")

    return props


# ── соц-сигнал X на входе vs исход (топливо A: коррелируем хайп с результатом) ──
def social_edge(records: list[dict], thr: int = 3,
                hard_stop: float = DEFAULT_HARD_STOP) -> dict:
    """Разбить закрытые сделки по числу X-упоминаний CA на входе (attrib['x_mentions'])
    относительно порога → «мало хайпа» vs «есть хайп». Пусто, пока сигнал не копился
    (x_mentions пишется только при подключённом Twitter, см. app._enrich_x_signal)."""
    trades = [t for t in closed_trades(records) if t["attrib"].get("x_mentions") is not None]
    if not trades:
        return {}
    lo, hi = _split(trades, "x_mentions", thr)
    return dict(threshold=thr, samples=len(trades),
                low=_stats([t["pnl"] for t in lo], hard_stop),
                high=_stats([t["pnl"] for t in hi], hard_stop))


# ── дайджест дня + полный прогон ─────────────────────────────────────────────
def _data_note(n: int) -> str:
    if n == 0:
        return "нет закрытых сделок — сначала накопи SELL-записи (SHADOW-бот наберёт топливо)"
    if n < MIN_TRADES:
        return f"данных мало ({n}<{MIN_TRADES}) — веса нейтральны, пороги не трогаем (не учимся на шуме)"
    return f"выборка {n} сделок — эдж/предложения активны"


def _summ(trades: list[dict], hs: float, fee_pct: float) -> dict:
    """Сводка по сделкам: реализованная статистика + вал/чистый SOL (минус round-trip cost)."""
    return _stats([t["pnl"] for t in trades], hs) | dict(
        total_sol=round(sum(t["sol"] for t in trades), 4),
        net_total_sol=round(sum((t["pnl"] - fee_pct) * t["size_sol"] for t in trades), 4))


def daily_report(records: list[dict], day: str | None = None, cfg: dict | None = None,
                 filters: dict | None = None, trigger: dict | None = None,
                 fee_pct: float = 0.0) -> dict:
    """Разбор дня: что сработало/нет + эдж кошельков/KOL + предложения (ничего не применяет).
    fee_pct — оценочный round-trip cost, вычитается из чистого PnL (net_total_sol)."""
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    hs = float((cfg or {}).get("hard_stop_pct", DEFAULT_HARD_STOP))
    day_trades = closed_trades([r for r in records if str(r.get("ts", "")).startswith(day)])
    all_trades = closed_trades(records)
    return dict(
        day=day, fee_pct=fee_pct,
        day_summary=_summ(day_trades, hs, fee_pct),
        overall=_summ(all_trades, hs, fee_pct),
        wallet_edge=wallet_edge(records, hard_stop=hs),
        kol_review=kol_review(),
        social_edge=social_edge(records, hard_stop=hs),
        proposals=propose(records, cfg, filters, trigger),
        note=_data_note(len(all_trades)),
    )


def run(records: list[dict], cfg: dict | None = None, filters: dict | None = None,
        trigger: dict | None = None, fee_pct: float = 0.0) -> dict:
    """Полный прогон: отчёт + веса кошельков для персиста (только выборки с достаточными данными)."""
    rep = daily_report(records, cfg=cfg, filters=filters, trigger=trigger, fee_pct=fee_pct)
    edges = {name: dict(weight=st["weight"], trades=st["trades"],
                        winrate=st["winrate"], expectancy_R=st["expectancy_R"])
             for name, st in rep["wallet_edge"].items() if st.get("sample_ok")}
    return dict(report=rep, edges=edges)


def summary(path=None, cfg: dict | None = None, filters: dict | None = None,
            trigger: dict | None = None, fee_pct: float = 0.0) -> dict:
    """Удобный вход для CLI/эндпоинта: сам читает журнал по пути."""
    return run(backtest.load_records(path), cfg=cfg, filters=filters, trigger=trigger, fee_pct=fee_pct)


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
