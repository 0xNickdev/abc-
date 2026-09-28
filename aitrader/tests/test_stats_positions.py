"""Аудит 28.09: статистика по ПОЗИЦИЯМ. Пример агента: реальный результат −0.106 SOL,
а старый расчёт давал win 0.6 / avg_R +1.28 (по записям) или −0.616 (только финалы)."""
import pytest

import backtest
import review


def _sell(addr, pnl, size, frac):
    return dict(action="SELL", address=addr, pnl=pnl, size_sol=size, fraction=frac, pubkey="local")


RECS = [
    _sell("A", 0.60, 0.40, 0.4), _sell("A", 1.50, 0.18, 0.3), _sell("A", 0.20, 0.42, 1.0),
    _sell("B", -0.35, 1.0, 1.0), _sell("C", -0.35, 1.0, 1.0),
]


def test_position_aggregation_matches_real_money():
    pos = backtest.positions_from_sells(RECS)
    assert len(pos) == 3
    a = pos[0]
    assert a["size_sol"] == pytest.approx(1.0) and a["sol"] == pytest.approx(0.594)
    assert a["pnl"] == pytest.approx(0.594) and a["legs"] == 3


def test_realized_sign_consistent_with_money():
    r = backtest.realized(RECS, hard_stop_pct=0.25)
    assert r["total_pnl_sol"] == pytest.approx(-0.106)
    assert r["trades"] == 3 and r["sell_records"] == 5
    assert r["win_rate"] == pytest.approx(0.333, abs=1e-3)
    assert r["expectancy_R"] < 0                      # раньше было +1.28R при минусе в SOL


def test_review_overall_counts_tp_profit():
    rep = review.daily_report(RECS, day="1999-01-01", cfg=dict(hard_stop_pct=0.25))
    assert rep["overall"]["total_sol"] == pytest.approx(-0.106)
    assert rep["overall"]["total_sol"] == pytest.approx(rep["cashflow"]["total_sol"])


def test_open_tail_not_counted_as_closed():
    pos = backtest.positions_from_sells([_sell("D", 0.6, 0.4, 0.4)])
    assert pos == []
