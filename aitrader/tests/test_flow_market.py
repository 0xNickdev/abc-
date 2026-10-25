"""25.10 «свой поток»: отбор кандидатов и цена входа мид-рынка из потока, а не из DexScreener
(отстаёт на 30–60 с). Из Dex — только возраст/тикер/h6/h24 и сверка «тот ли пул»."""
import time

import pytest

import app as appmod
import pumpflow

MINT = "Flw1111111111111111111111111111111111111pump"


@pytest.fixture
def flow(monkeypatch):
    f = pumpflow.PumpFlow()
    f._started_at = time.time() - 7200                       # поток видел больше часа
    mf = f._get(MINT, time.time())
    mf.venue, mf.price_sol = "amm", 2e-6
    mf.pool_px["P"] = (2e-6, int(150e9), int(75e15), time.time())   # 150 SOL на стороне SOL
    now = time.time()
    mf.buckets.append([now - 600, 40, 20, int(30e9), int(10e9)])
    mf.candles.extend([[now - 4000, 1.6e-6, 1.6e-6, 1.6e-6, 1.6e-6, 0], [now - 120, 1.9e-6, 2e-6, 1.9e-6, 2e-6, 0]])
    monkeypatch.setattr(pumpflow, "FLOW", f)
    monkeypatch.setattr(appmod.pumpflow, "FLOW", f)
    return f


def test_change_pct_from_candles(flow):
    assert flow.change_pct(MINT, 3600) == 25.0               # 1.6e-6 → 2e-6
    flow._started_at = time.time() - 60
    assert flow.change_pct(MINT, 3600) is None               # поток моложе окна


def test_flow_market_fields(flow):
    fl = appmod._flow_market(MINT, 100.0)
    assert fl["price"] == pytest.approx(2e-4) and fl["mcap"] == pytest.approx(200_000)
    assert fl["liq"] == pytest.approx(30_000) and fl["dex"] == "pumpswap"
    assert fl["buys_h1"] == 40 and fl["sells_h1"] == 20 and fl["vol_h1"] == pytest.approx(4000)
    assert fl["chg_h1"] == 25.0


def test_flow_market_skips_hourly_when_not_full(flow):
    flow._started_at = time.time() - 60
    flow._m[MINT].created_ts = 0
    fl = appmod._flow_market(MINT, 100.0)
    assert "buys_h1" not in fl and "chg_h1" not in fl and fl["mcap"] == pytest.approx(200_000)


def test_mid_market_flow_over_dex(flow):
    dexd = dict(price=1.5e-4, mcap=150_000, liq=20_000, age_h=3.0, sym="FLW", chg_h1=5, chg_h24=80, buys_h1=10)
    d = appmod._mid_market(MINT, dexd, 100.0)
    assert d["mcap"] == pytest.approx(200_000) and d["chg_h1"] == 25.0 and d["buys_h1"] == 40   # быстрое — поток
    assert d["age_h"] == 3.0 and d["sym"] == "FLW" and d["chg_h24"] == 80                       # медленное — Dex
    assert d["dex_price"] == 1.5e-4


def test_mid_buy_uses_flow_price_without_dexscreener(flow, tmp_path, monkeypatch):
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    s = appmod.ST
    monkeypatch.setattr(s, "positions", [])
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(appmod.dexadapter, "spot_pair", lambda a: pytest.fail("DexScreener not needed"))
    monkeypatch.setattr(appmod.dexadapter, "spot_price", lambda a: pytest.fail("DexScreener not needed"))
    monkeypatch.setitem(appmod._MID_DEX, MINT, (time.time(), dict(sym="FLW")))
    appmod.do_buy("sol", MINT, 0.3, s, market="mid")
    p = next(p for p in s.positions if p["address"] == MINT)
    assert p["symbol"] == "FLW" and p["sim_entry"]["mid"] == pytest.approx(2e-4)
    assert p["sim_entry"]["dex"] == "pumpswap" and p["entry_liq"] == pytest.approx(30_000)
