"""Контрольный замер (25.10): PnL считается только с MEASURE_SINCE; история в журнале цела."""
import json

import backtest


def _log(tmp_path, rows):
    p = tmp_path / "trade_decisions.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def test_summary_since_filters_sells(tmp_path):
    rows = [dict(ts="2026-10-24T10:00:00+00:00", action="SELL", symbol="OLD", pnl=-0.5, size_sol=0.3,
                 reason="closed PnL -50%", address="a"),
            dict(ts="2026-10-25T10:00:00+00:00", action="SELL", symbol="NEW", pnl=0.1, size_sol=0.3,
                 reason="closed PnL +10%", address="b")]
    p = _log(tmp_path, rows)
    full = backtest.summary(path=p)
    since = backtest.summary(path=p, since="2026-10-25")
    assert full["realized"]["trades"] == 2 and since["realized"]["trades"] == 1
    assert since["measure_since"] == "2026-10-25"
    assert since["realized"]["total_pnl_sol"] > 0 and full["realized"]["total_pnl_sol"] < 0
    assert since["records"] == full["records"]                    # журнал не обрезан
