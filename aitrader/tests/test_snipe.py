"""27.10 снайп: отдельный ранний рынок (свежие токены, ниже капа, возраст минуты, вход без
задержки) через тот же сканёр своего потока, что mid. Профиль из _markets."""
import time

import pytest

import app as appmod
import bot


def test_markets_profiles():
    mk = appmod._markets()
    assert set(mk) == {"mid", "snipe"}
    assert mk["snipe"]["label"] == "snipe" and mk["snipe"]["delay_s"] == 0.0
    assert mk["snipe"]["min_mcap"] < mk["mid"]["min_mcap"]       # снайп — ниже капа
    assert mk["snipe"]["max_age_h"] < mk["mid"]["max_age_h"]     # и моложе


def test_screen_snipe_uses_snipe_bounds(monkeypatch):
    from test_mid_market import mid  # noqa
    # перенастроим fake-данные под снайп-диапазон
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    flow = appmod.pumpflow.FLOW
    base = dict(price=1e-4, mcap=150_000, liq=20_000, age_h=0.3, vol_h1=1000, sym="SNP")
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {m: base for m in ms})
    monkeypatch.setattr(appmod, "_flow_market", lambda m, r: None)   # чисто Dex-поля в тесте
    monkeypatch.setattr(appmod.ST, "positions", [])
    monkeypatch.setattr(appmod.agents, "provider", lambda: None)
    out = appmod.screen_snipe("sol", appmod.ST)
    if out["decisions"]:
        assert out["decisions"][0]["decision"]["features"]["market"] == "snipe"
        assert "snipe" in out["decisions"][0]["decision"]["reason"]


def test_snipe_market_switch_valid(tmp_path, monkeypatch):
    from test_app import _mu_client
    c = _mu_client(tmp_path, monkeypatch)
    pk = "Usr3333333333333333333333333333333333333333"
    assert c.post("/api/bot/config", json={"market": "snipe"}, headers={"X-Wallet": pk}).status_code == 200
    assert appmod.get_session(pk).bot.cfg["market"] == "snipe"
    assert c.post("/api/bot/config", json={"market": "weird"}, headers={"X-Wallet": pk}).status_code == 400


def test_snipe_position_uses_mid_exits():
    rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[])
    pos = dict(market="snipe", pnl=-0.25, peak_pnl=0.0, opened_ts=time.time() - 60, tp_taken=[])
    assert bot.decide_exit(pos, None, rc).reason.startswith("Mid stop")   # снайп = те же выходы
