"""Results по позициям: частичные продажи + закрытие = одна строка; исключение сбоев."""
import datetime
import json

import pytest

import app as appmod
from test_app import _mu_client

A1 = "PosOneAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
A2 = "PosTwoAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"


@pytest.fixture
def client(tmp_path, monkeypatch):
    c = _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod, "RESULTS_SINCE", "")
    return c


def _sell(sym, addr, pnl, size, frac, ts):
    rec = dict(ts=ts, action="SELL", symbol=sym, reason="x", mode="SHADOW", pubkey="local",
               pnl=pnl, pnl_gross=pnl + 0.05, size_sol=size, address=addr, fraction=frac, costed=True)
    with appmod.LOG_PATH.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _month():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _ts(h):
    return f"{_month()}-05T{h:02d}:00:00+00:00"


def test_partials_and_final_close_are_one_row(client):
    _sell("WIN", A1, 0.6, 0.04, 0.4, _ts(1))     # TP1
    _sell("WIN", A1, 1.5, 0.03, 0.5, _ts(2))     # TP2
    _sell("WIN", A1, -0.1, 0.03, 1.0, _ts(3))    # остаток по трейлингу
    _sell("LOSS", A2, -0.3, 0.1, 1.0, _ts(4))
    cal = client.get("/api/pnl/calendar", params={"month": _month()}).json()
    rows = next(iter(cal["days"].values()))["list"]
    assert len(rows) == 2 and cal["stats"]["trades"] == 2
    win = next(r for r in rows if r["sym"] == "WIN")
    assert win["legs"] == 3 and win["open"] is False
    assert win["net"] == pytest.approx(0.6 * 0.04 + 1.5 * 0.03 - 0.1 * 0.03)
    assert win["pct"] == pytest.approx(win["net"] / 0.1)
    assert cal["stats"]["win_rate"] == 0.5


def test_partly_sold_open_position_is_shown_as_open(client):
    _sell("RUN", A1, 0.6, 0.04, 0.4, _ts(1))
    cal = client.get("/api/pnl/calendar", params={"month": _month()}).json()
    row = next(iter(cal["days"].values()))["list"][0]
    assert row["open"] is True and row["legs"] == 1


def test_excluded_glitch_trade_is_not_counted(client):
    _sell("GLITCH", A1, -1.0, 0.1, 1.0, _ts(6))
    _sell("REAL", A2, 0.2, 0.1, 1.0, _ts(7))
    (appmod.OUT_DIR / "pnl_exclude.json").write_text(json.dumps(
        [dict(address=A1, ts=_ts(6)[:16], reason="price glitch")]))
    cal = client.get("/api/pnl/calendar", params={"month": _month()}).json()
    assert [r["sym"] for r in next(iter(cal["days"].values()))["list"]] == ["REAL"]
    assert cal["excluded"] == 1 and cal["stats"]["net_total_sol"] == pytest.approx(0.02)
