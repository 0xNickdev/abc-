"""Модель издержек бумажной торговли: вход дороже рынка, выход дешевле, фикс-издержки (priority/base
fee, рента ATA), стопы проскальзывают сильнее; календарь не вычитает процент повторно."""
import datetime

import pytest
from test_app import _mu_client

import app as appmod


def test_entry_costs_raise_effective_price(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    eff, fixed, parts = appmod.sim_entry("XYZpump", 1.0, 0.1, 10_000.0)      # кривая, $10k ликвидности
    assert parts["fee"] == appmod.SIM_FEE_CURVE and parts["impact"] == pytest.approx(0.002)
    assert eff == pytest.approx(1.0 + appmod.SIM_FEE_CURVE + 0.002 + appmod.SIM_SLIP)
    assert fixed == pytest.approx(appmod.SIM_TX_FEE_SOL + appmod.SIM_ATA_RENT_SOL)


def test_flat_price_round_trip_loses_money(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    p = dict(address="AMMTOKEN", entry_liq=200_000.0, sim_fixed_sol=0.00229)
    net, cost = appmod.sim_exit(p, 0.0, 1.0, 0.1, "Trailing")
    assert -0.06 < net < -0.03 and cost > 0                 # «в ноль по цене» = минус на издержках


def test_stop_slips_more(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    p = dict(address="AMMTOKEN", entry_liq=200_000.0, sim_fixed_sol=0.0)
    n_tp, _ = appmod.sim_exit(p, -0.25, 1.0, 0.1, "Trailing")
    n_stop, _ = appmod.sim_exit(p, -0.25, 1.0, 0.1, "Hard stop PnL -25%")
    assert n_stop < n_tp


def test_paper_trade_is_costed_and_calendar_no_double_fee(tmp_path, monkeypatch):
    _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    s = appmod.ST
    addr = appmod.MK.trending_rows("sol")[0]["address"]
    appmod.do_buy("sol", addr, 0.1, s)
    p = s.positions[-1]
    assert p["sim_entry"] and p["entry_price"] > p["cur_price"]
    p["pnl"] = 0.0
    appmod.do_sell(addr, 1.0, "Trailing", s)
    rec = [ln for ln in appmod.LOG_PATH.read_text().splitlines() if '"SELL"' in ln][-1]
    assert '"costed": true' in rec and '"pnl_gross": 0.0' in rec
    m = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    cal = appmod.pnl_calendar("local", m)
    day = next(iter(cal["days"].values()))
    assert day["pnl"] == 0.0 and day["net"] < 0                # вал 0, чистый = модельные издержки
    assert day["net"] > -0.1 * 0.07                            # и без повторных ABC_FEE_PCT
