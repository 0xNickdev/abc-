"""Рынок от $200k (16.10): выходы +30%/−20%/4 ч, скан — моложе суток, ликвидность, цена потока ≈ Dex,
чужой пул отсекается; покупка без GMGN (цена DexScreener); переключатель market."""
import time

import pytest

import app as appmod
import bot


class TestMidExits:
    def pos(self, pnl, age_s=60):
        return dict(market="mid", pnl=pnl, peak_pnl=max(pnl, 0), opened_ts=time.time() - age_s, tp_taken=[])

    def test_tp_sl_time(self):
        rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[(0.6, 0.4)])
        assert bot.decide_exit(self.pos(0.31), None, rc).action == "SELL"
        assert bot.decide_exit(self.pos(-0.21), None, rc).reason.startswith("Mid stop")
        assert bot.decide_exit(self.pos(0.10), None, rc).action == "HOLD"
        assert "time exit" in bot.decide_exit(self.pos(0.10, age_s=4 * 3600 + 5), None, rc).reason

    def test_fresh_positions_keep_old_rules(self):
        rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[(0.6, 0.4)])
        assert bot.decide_exit(dict(pnl=0.31, peak_pnl=0.31, tp_taken=[]), None, rc).action == "HOLD"


class FakeFlow:
    def __init__(self, px):
        self.px = px

    def mid_candidates(self, a, b):
        return [(m, 0, 0) for m in self.px]

    def price(self, m):
        return (self.px[m], time.time()) if m in self.px else None

    def metrics(self, m, w=300):
        return dict(symbol=m[:4], pool_canonical=(m != "Foreign"), buy_ratio=0.6, unique_buyers=10)


@pytest.fixture
def mid(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    flow = FakeFlow({"Good": 1e-6, "Old": 1e-6, "Thin": 1e-6, "Foreign": 1e-6, "Off": 1e-6, "Small": 1e-6})
    monkeypatch.setattr(appmod.pumpflow, "FLOW", flow)
    base = dict(price=1e-4, mcap=500_000, liq=50_000, age_h=5.0, vol_h1=1000)
    dex = {"Good": base, "Old": dict(base, age_h=30), "Thin": dict(base, liq=5_000), "Foreign": base,
           "Off": dict(base, price=1e-3), "Small": dict(base, mcap=100_000)}
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {m: dex[m] for m in ms if m in dex})
    import venues
    monkeypatch.setattr(venues.WATCH, "mints", lambda: [])
    return appmod.ST


def test_screen_mid_filters(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [])
    out = appmod.screen_mid("sol", mid)
    assert [d["decision"]["address"] for d in out["decisions"]] == ["Good"]
    dec = out["decisions"][0]["decision"]
    assert dec["action"] == "ACTION" and dec["abc"]["triggered"] and dec["features"]["market"] == "mid"


def test_screen_skips_held(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [dict(address="Good", size_sol=0.2, chain="sol")])
    assert appmod.screen_mid("sol", mid)["decisions"] == []


def test_mid_buy_uses_dexscreener_not_gmgn(tmp_path, monkeypatch):
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    s = appmod.ST
    monkeypatch.setattr(s, "positions", [])
    g = s.adapter_for("sol")
    monkeypatch.setattr(g, "token_info", lambda a: pytest.fail("GMGN must not be called for mid"))
    monkeypatch.setattr(appmod.dexadapter, "spot_pair", lambda a: dict(baseToken=dict(symbol="MID"), priceUsd="0.002"))
    monkeypatch.setattr(appmod.dexadapter, "spot_price", lambda a: 0.002)
    appmod.do_buy("sol", "MidTok1111", 0.2, s, market="mid")
    p = next(p for p in s.positions if p["address"] == "MidTok1111")
    assert p["market"] == "mid" and p["entry_price"] == pytest.approx(0.002, rel=0.05)   # + симуляция проскальзывания


def test_market_switch_validation(tmp_path, monkeypatch):
    from test_app import _mu_client
    c = _mu_client(tmp_path, monkeypatch)
    pk = "Usr2222222222222222222222222222222222222222"
    assert c.post("/api/bot/config", json={"market": "weird"}, headers={"X-Wallet": pk}).status_code == 400
    r = c.post("/api/bot/config", json={"market": "mid"}, headers={"X-Wallet": pk})
    assert r.status_code == 200 and appmod.get_session(pk).bot.cfg["market"] == "mid"
