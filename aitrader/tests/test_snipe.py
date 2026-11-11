"""27.10 снайп: отдельный ранний рынок (свежие токены, ниже капа, возраст минуты, вход без
задержки) через тот же сканёр своего потока, что mid. Профиль из _markets."""
import time

import pytest

import app as appmod

import bot


def test_markets_profiles():
    mk = appmod._markets()
    assert set(mk) == {"mid", "snipe", "migration"}
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


def test_curve_candidates_fresh_only():
    import pumpflow
    f = pumpflow.PumpFlow()
    now = time.time()
    # свежий на кривой в диапазоне возраста/капы → кандидат
    good = f._get("Crv1111111111111111111111111111111111111pump", now)
    good.venue, good.price_sol, good.created_ts, good.last_ts = "curve", 5e-5, now - 600, now
    good.real_sol = int(20e9)
    # слишком молодой (<5 мин) — нет окна спроса
    young = f._get("Crv2222222222222222222222222222222222222pump", now)
    young.venue, young.price_sol, young.created_ts, young.last_ts = "curve", 5e-5, now - 60, now
    young.real_sol = int(20e9)
    # мигрировал (complete) — это уже mid, не кривая
    mig = f._get("Crv3333333333333333333333333333333333333pump", now)
    mig.venue, mig.price_sol, mig.created_ts, mig.last_ts, mig.complete = "curve", 5e-5, now - 600, now, True
    out = [c[0] for c in f.curve_candidates(1, 1e12, 1, min_age_s=300, max_age_s=3600)]
    assert "Crv1111111111111111111111111111111111111pump" in out
    assert "Crv2222222222222222222222222222222222222pump" not in out
    assert "Crv3333333333333333333333333333333333333pump" not in out


def test_snipe_profile_has_curve():
    mk = appmod._markets()["snipe"]
    assert mk.get("curve") is True and mk["curve_min_age_s"] == 300
    assert appmod._markets()["mid"].get("curve") is None   # mid кривую не трогает
