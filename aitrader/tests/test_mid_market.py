"""Рынок бота: выходы HOTBOT (x2 → половина на откате 15%, остаток трейлинг), −20%/4 ч; правила HOTBOT; скан — моложе суток, ликвидность, цена потока ≈ Dex,
чужой пул отсекается; покупка без GMGN (цена DexScreener); переключатель market."""
import time

import pytest

import app as appmod
import bot


class TestMidExits:
    def pos(self, pnl, age_s=60, peak=None, taken=()):
        return dict(market="mid", pnl=pnl, peak_pnl=max(pnl, 0) if peak is None else peak,
                    opened_ts=time.time() - age_s, tp_taken=list(taken))

    rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[(0.6, 0.4)])

    def test_sl_time_and_no_quick_take(self):
        rc = self.rc
        assert bot.decide_exit(self.pos(0.31), None, rc).action == "HOLD"        # HOTBOT: +30% больше не выход
        assert bot.decide_exit(self.pos(-0.21), None, rc).reason.startswith("Mid stop")
        assert bot.decide_exit(self.pos(0.10), None, rc).action == "HOLD"
        assert "time exit" in bot.decide_exit(self.pos(0.10, age_s=4 * 3600 + 5), None, rc).reason

    def test_hotbot_half_on_pullback_after_x2(self):
        rc = self.rc
        assert bot.decide_exit(self.pos(1.2), None, rc).action == "HOLD"          # растёт — держим
        ed = bot.decide_exit(self.pos(0.85, peak=1.2), None, rc)                  # откат 16% от пика +120%
        assert ed.action == "SELL" and ed.fraction == 0.5 and ed.rung == 0
        assert bot.decide_exit(self.pos(0.95, peak=1.2), None, rc).action == "HOLD"   # откат 11%
        assert bot.decide_exit(self.pos(0.5, peak=0.9), None, rc).action == "HOLD"    # до x2 не дошли

    def test_runner_on_trailing_after_half(self):
        rc = self.rc
        after = dict(taken=[0], age_s=6 * 3600)                                   # время/стоп больше не действуют
        assert bot.decide_exit(self.pos(1.5, peak=2.0, **after), None, rc).action == "HOLD"   # откат 17%
        ed = bot.decide_exit(self.pos(1.2, peak=2.0, **after), None, rc)                      # откат 27%
        assert ed.action == "SELL" and ed.fraction == 1.0 and "trailing" in ed.reason

    def test_fresh_positions_keep_old_rules(self):
        rc = self.rc
        assert bot.decide_exit(dict(pnl=0.31, peak_pnl=0.31, tp_taken=[]), None, rc).action == "HOLD"


class TestHotbotRules:
    base = dict(chg_h1=40, buys_h1=200, sells_h1=100)

    def test_pass_with_unknown_features(self):
        assert appmod._hotbot_rules(self.base, {}) is None
        assert appmod._hotbot_rules({}, {}) is None                               # всё None — не режем

    @pytest.mark.parametrize("d,snap,fund,why", [
        (dict(chg_h1=2), {}, None, "chg_h1"),
        (dict(chg_h1=900), {}, None, "chg_h1"),
        (dict(buys_h1=30, sells_h1=10), {}, None, "buyers_h1"),
        (dict(buys_h1=110, sells_h1=100), {}, None, "buy/sell"),
        ({}, dict(dd_from_high_pct=55), None, "dd_from_high_pct"),
        ({}, dict(crash_1m_max_pct=35), None, "crash_1m_max_pct"),
        ({}, dict(launch_x10s=3.5), None, "launch_x10s"),
        ({}, dict(dev_supply_pct=12), None, "dev_supply_pct"),
        ({}, dict(launch_bundle_pct=15), None, "launch_bundle_pct"),
        ({}, dict(sniper_supply_pct=11), None, "sniper_supply_pct"),
        ({}, dict(holders_obs=60), None, "holders_obs"),
        ({}, dict(dev_prev_rugs=1), None, "dev_prev_rugs"),
        ({}, {}, dict(insiders_pct=14), "insiders_pct"),
        ({}, {}, dict(bundle_pct=20), "bundle_pct"),
    ])
    def test_each_rule_cuts(self, d, snap, fund, why):
        assert appmod._hotbot_rules(dict(self.base, **d), snap, fund).startswith(why)

    def test_flow_unique_buyers_override_dex(self):
        assert appmod._hotbot_rules(self.base, {}, None, uniq_h1=20).startswith("buyers_h1")
        assert appmod._hotbot_rules(dict(self.base, buys_h1=60, sells_h1=10), {}, None, uniq_h1=80) is None

    def test_brand_tickers(self):
        for s in ("YouTube", "ROBINHOOD", "McRib", "McNugget", "ELON", "Claude AI", "TRUMP2028", "x"):
            assert appmod._brand_ticker(s), s
        for s in ("ヤギ", "BONKAI", "Hello", "MOODENG"):
            assert not appmod._brand_ticker(s), s


class FakeFlow:
    def __init__(self, px):
        self.px = px

    def mid_candidates(self, a, b):
        return [(m, 0, 0) for m in self.px]

    def price(self, m):
        return (self.px[m], time.time()) if m in self.px else None

    def entry_snapshot(self, m):
        return {"5m": dict(b=10, s=5)}

    def snapshot(self):
        return dict(uptime_s=getattr(self, "up", 3600))

    def metrics(self, m, w=300):
        return dict(symbol=m[:4], pool_canonical=(m != "Foreign"), buy_ratio=0.6, unique_buyers=10)


@pytest.fixture
def mid(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    flow = FakeFlow({"Huge": 1e-6, "Good": 1e-6, "Old": 1e-6, "Thin": 1e-6, "Foreign": 1e-6, "Off": 1e-6, "Small": 1e-6})
    monkeypatch.setattr(appmod.pumpflow, "FLOW", flow)
    base = dict(price=1e-4, mcap=1_500_000, liq=50_000, age_h=5.0, vol_h1=1000)
    base = dict(base, sym="Good")
    dex = {"Good": base, "Old": dict(base, age_h=100), "Thin": dict(base, liq=5_000), "Foreign": base,
           "Off": dict(base, price=1e-3), "Small": dict(base, mcap=100_000), "Huge": dict(base, mcap=9_000_000)}
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {m: dex[m] for m in ms if m in dex})
    import venues
    monkeypatch.setattr(venues.WATCH, "mints", lambda: [])
    monkeypatch.setattr(appmod.rugmodel, "score", lambda d, f: 0.0)
    monkeypatch.setattr(appmod.rugmodel, "threshold", lambda: 0.5)
    monkeypatch.setattr(appmod, "MID_ENTRY_DELAY_S", 0)
    monkeypatch.setattr(appmod.agents, "provider", lambda: None)   # без реальных LLM-вызовов в тестах
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


def test_paper_house_loss_does_not_drain_real_money_pot(tmp_path, monkeypatch):
    """16.10: бумажные потери house-бота копились в общем недельном котле и блокировали ботов юзеров."""
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    s = appmod.ST
    monkeypatch.setattr(s, "mode", "SHADOW")
    monkeypatch.setattr(appmod.WEEK_BUDGET, "loss", 0.0)
    s.positions[:] = [dict(symbol="L", address="LossTok", size_sol=0.3, pnl=-0.5, entry_price=1.0,
                           cur_price=0.5, chain="sol", entry={}, cycles=0)]
    appmod.do_sell("LossTok", 1.0, "test", s)
    assert appmod.WEEK_BUDGET.loss == 0.0


def test_rug_model_cuts_risky(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [])
    monkeypatch.setattr(appmod.rugmodel, "score", lambda d, f: 0.9)
    assert appmod.screen_mid("sol", mid)["decisions"] == []


def test_real_rug_model_loads_and_scores():
    import rugmodel
    rugmodel.load()
    p = rugmodel.score(dict(mcap=500_000, liq=50_000, age_h=5, vol_h1=10_000), {"5m": dict(b=50, s=40, uniq=30)})
    assert p is not None and 0.0 <= p <= 1.0 and rugmodel.threshold() > 0


def test_rugged_symbol_and_copycat_filters(mid, monkeypatch, tmp_path):
    monkeypatch.setattr(mid, "positions", [])
    monkeypatch.setattr(appmod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "_RUG_SYMS", {"good": __import__("time").time()})
    assert appmod.screen_mid("sol", mid)["decisions"] == []          # тикер Good уже сливался
    appmod._RUG_SYMS.clear(); appmod._RUG_SYMS["other"] = 0.0
    assert [d["decision"]["address"] for d in appmod.screen_mid("sol", mid)["decisions"]] == ["Good"]


def test_no_buys_right_after_restart(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [])
    appmod.pumpflow.FLOW.up = 30
    assert appmod.screen_mid("sol", mid)["decisions"] == []


def test_entry_delay_after_first_seen(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [])
    monkeypatch.setattr(appmod, "MID_ENTRY_DELAY_S", 1800)
    monkeypatch.setattr(appmod, "_MID_FIRST", {})
    assert appmod.screen_mid("sol", mid)["decisions"] == []                     # только что появился
    appmod._MID_FIRST["Good"] = time.time() - 1900
    assert [d["decision"]["address"] for d in appmod.screen_mid("sol", mid)["decisions"]] == ["Good"]


def test_brand_ticker_cut_in_screen(mid, monkeypatch):
    monkeypatch.setattr(mid, "positions", [])
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {"Good": dict(price=1e-4, mcap=1_500_000, liq=50_000,
                                                                      age_h=5.0, vol_h1=1000, sym="YouTube")})
    assert appmod.screen_mid("sol", mid)["decisions"] == []
