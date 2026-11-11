"""11.11 Migration bot: отдельный рынок — покупка на кривой перед миграцией (в кривой 75–80 SOL),
продажа сразу после миграции, иначе стоп/время. Включается выбором стратегии migration_v1."""
import time

import pytest

import app as appmod
import bot
import pumpflow
import strategy

MINT = "Mig1111111111111111111111111111111111111pump"


def _curve(f, mint, real_sol, age_s=900, odd=False):
    now = time.time()
    mf = f._get(mint, now)
    mf.venue, mf.created_ts, mf.last_ts = "curve", now - age_s, now
    mf.real_sol = int(real_sol * 1e9)
    mf.price_sol = (30 + real_sol) ** 2 / (30 * 1.073e9)        # цена стандартной кривой
    mf.odd_curve = odd
    return mf


def test_curve_candidates_near_migration_window():
    f = pumpflow.PumpFlow()
    _curve(f, "Low1111111111111111111111111111111111111pump", 40)
    _curve(f, MINT, 77)
    _curve(f, "Hi11111111111111111111111111111111111111pump", 83)
    _curve(f, "Odd1111111111111111111111111111111111111pump", 77, odd=True)     # mayhem-кривая
    out = [c[0] for c in f.curve_candidates(0, 1e12, 0, min_age_s=300, max_age_s=86400,
                                            min_real_sol=75, max_real_sol=80, std_only=True)]
    assert out == [MINT]


def test_standard_curve_invariant_in_bounds():
    lo, hi = pumpflow.STD_CURVE_K
    assert lo < int(30e9) * int(1.073e15) < hi                  # старт стандартной кривой
    assert not lo < int(30e9) * int(2.146e15) < hi              # саплай 2 млрд (mayhem) — мимо


def test_graduated():
    f = pumpflow.PumpFlow()
    mf = _curve(f, MINT, 77)
    assert f.graduated(MINT) is False
    mf.complete = True
    assert f.graduated(MINT) is True
    assert f.graduated("unknown") is False


class TestMigrationExits:
    rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[(0.6, 0.4)])

    def pos(self, pnl, age_s=60, **kw):
        return dict(market="migration", pnl=pnl, peak_pnl=max(pnl, 0), opened_ts=time.time() - age_s,
                    tp_taken=[], **kw)

    def test_hold_until_something_happens(self):
        assert bot.decide_exit(self.pos(0.08), None, self.rc).action == "HOLD"
        assert bot.decide_exit(self.pos(-0.10), None, self.rc).action == "HOLD"
        assert bot.decide_exit(self.pos(0.70), None, self.rc).action == "HOLD"     # лестница тейков не действует

    def test_sell_on_graduation(self):
        ed = bot.decide_exit(self.pos(0.12, migrated=True), None, self.rc)
        assert ed.action == "SELL" and ed.fraction == 1.0 and ed.reason.startswith("Migration exit")

    def test_stop_and_time(self):
        assert bot.decide_exit(self.pos(-0.16), None, self.rc).reason.startswith("Migration stop")
        assert "time exit" in bot.decide_exit(self.pos(0.02, age_s=1205), None, self.rc).reason

    def test_shock_exit(self):
        ed = bot.decide_exit(self.pos(-0.05, flow_shock=(time.time(), 0.2, 0.1)), None, self.rc)
        assert ed.action == "SELL" and "shock" in ed.reason


def test_strategy_sets_bot_market(tmp_path, monkeypatch):
    from test_app import _mu_client
    c = _mu_client(tmp_path, monkeypatch)
    pk = "Usr4444444444444444444444444444444444444444"
    sess = appmod.get_session(pk)
    assert appmod._bot_market(sess) == sess.bot.cfg["market"]
    assert c.post("/api/strategy/select", json={"id": "migration_v1"}, headers={"X-Wallet": pk}).status_code == 200
    assert appmod._bot_market(sess) == "migration"
    seen = []
    monkeypatch.setattr(appmod, "screen_market", lambda ch, s, mkt: seen.append(mkt) or {})
    appmod._screen_for(sess)("sol")
    assert seen == ["migration"]
    # витрина: карточка с фиксированными выходами
    pub = c.get("/api/strategies?view=public", headers={"X-Wallet": pk}).json()
    card = next(s for s in pub["strategies"] if s["id"] == "migration_v1")
    assert card["name"] == "Migration bot" and card["exit_note"] and card["exits"]["stop_pct"] == 0.15
    # обратно на обычную стратегию — рынок снова из настроек бота
    c.post("/api/strategy/select", json={"id": "abc_alpha_v1"}, headers={"X-Wallet": pk})
    assert appmod._bot_market(sess) == sess.bot.cfg["market"]
    assert c.post("/api/bot/config", json={"market": "migration"}, headers={"X-Wallet": pk}).status_code == 200


def test_migration_profile():
    mk = appmod._markets()["migration"]
    assert mk["curve"] and mk["amm"] is False and mk["agents"] is False and mk["std_curve"]
    assert mk["curve_min_sol"] < mk["curve_max_sol"] < 85
    assert strategy.get("migration_v1")["market"] == "migration"
    assert "market" not in strategy.get("abc_alpha_v1")


def test_migration_scan_and_paper_buy(tmp_path, monkeypatch):
    """Скан берёт только кривую в окне, покупка идёт по цене потока с комиссией кривой."""
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    f = pumpflow.PumpFlow()
    f._started_at = time.time() - 7200
    monkeypatch.setattr(pumpflow, "FLOW", f)
    monkeypatch.setattr(appmod.pumpflow, "FLOW", f)
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {})                     # у кривой пары на Dex нет
    monkeypatch.setattr(appmod.rugmodel, "score", lambda d, snap: None)
    monkeypatch.setattr(appmod.zooai, "enabled", lambda: False)
    monkeypatch.setattr(appmod.agents.TEAM, "request", lambda *a, **k: pytest.fail("agents not used"))
    now = time.time()
    mf = _curve(f, MINT, 77)
    mf.symbol = "MIG"
    mf.buckets.append([now - 60, 30, 5, int(12e9), int(2e9)])
    for i in range(20):
        mf.users[f"buyer{i}"] = now - 30
    amm = f._get("Amm1111111111111111111111111111111111111pump", now)            # PumpSwap-токен — не наш рынок
    amm.venue, amm.price_sol, amm.complete = "amm", 2e-6, True
    amm.pool_px["P"] = (2e-6, int(150e9), int(75e15), now)
    s = appmod.ST
    monkeypatch.setattr(s, "positions", [])
    out = appmod.screen_market("sol", s, "migration")
    assert [d["decision"]["address"] for d in out["decisions"]] == [MINT]
    feats = out["decisions"][0]["decision"]["features"]
    assert feats["market"] == "migration" and feats["curve_sol"] == pytest.approx(77)
    monkeypatch.setattr(appmod.dexadapter, "spot_pair", lambda a: pytest.fail("DexScreener not needed"))
    monkeypatch.setattr(appmod.dexadapter, "spot_price", lambda a: pytest.fail("DexScreener not needed"))
    appmod.do_buy("sol", MINT, 0.5, s, market="migration")
    p = next(p for p in s.positions if p["address"] == MINT)
    assert p["market"] == "migration" and p["sim_entry"]["dex"] == "pumpfun"
    assert p["sim_entry"]["fee"] == appmod.SIM_FEE_CURVE


def test_migration_is_paper_only(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    sess = appmod.get_session("Usr5555555555555555555555555555555555555555")
    sess.set_strategy("migration_v1")
    monkeypatch.setattr(sess, "mode", "LIVE")
    with pytest.raises(HTTPException) as e:
        appmod._bot_buy_fn(sess)("sol", MINT, 0.5)
    assert e.value.status_code == 409 and "paper-only" in e.value.detail
