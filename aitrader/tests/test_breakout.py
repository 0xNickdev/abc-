"""11.11 Breakout bot (механика HOTBOT): вход на пробое тихого диапазона при живой торговле,
выход — именованный план над трейлингом. Включается выбором стратегии breakout_v1."""
import time

import pytest

import app as appmod
import bot
import pumpflow
import strategy

MINT = "Brk1111111111111111111111111111111111111pump"


def _flow(prices, px, buyers=4, trades=12):
    """Токен с минутными свечами prices (хай=лоу=цена) за прошлые минуты и текущей ценой px."""
    f = pumpflow.PumpFlow()
    now = time.time()
    mf = f._get(MINT, now)
    cur = int(now // 60 * 60)
    n = len(prices)
    for i, p in enumerate(prices):
        mf.candles.append([cur - (n - i) * 60, p, p, p, p, 0.0])
    mf.price_sol, mf.last_ts, mf.venue = px, now, "amm"
    for i in range(trades):                       # сделки равномерно по последней минуте
        mf.recent.append((now - i * 5 - 0.5, 1, True, int(1e9), f"w{i % buyers}"))
    return f


def test_breakout_signal():
    quiet = [1.00, 1.02, 1.01, 1.03, 1.00, 1.02, 1.01, 1.02, 1.03, 1.01, 1.02]
    b = _flow(quiet, 1.05).breakout(MINT, 10, 10, 3)
    assert b["ok"] and b["quiet"] and b["broke"] and b["buyers"] == 4 and b["over_pct"] == pytest.approx(1.94, abs=0.01)
    assert not _flow(quiet, 1.02).breakout(MINT, 10, 10, 3)["ok"]                 # не пробил максимум
    wide = [1.0, 1.3] + quiet[2:]
    b = _flow(wide, 1.4).breakout(MINT, 10, 10, 3)
    assert b["broke"] and not b["quiet"] and not b["ok"]                          # диапазон не тихий
    assert not _flow(quiet, 1.05, buyers=2).breakout(MINT, 10, 10, 3)["ok"]       # мало покупателей
    assert _flow(quiet[:4], 1.05).breakout(MINT, 10, 10, 3) is None               # свечи не покрывают окно


def test_active_trading():
    assert _flow([1.0], 1.0, trades=12).active_trading(MINT, 15, 9, 3)["ok"]
    a = _flow([1.0], 1.0, trades=4).active_trading(MINT, 15, 9, 3)
    assert a["buckets"] == 4 and not a["ok"]
    f = _flow([1.0], 1.0)
    f._m[MINT].last_ts = time.time() - 40
    assert not f.active_trading(MINT, 15, 9, 3)["ok"]                             # последняя сделка давно


class TestPlans:
    def pos(self, pnl, peak=None, age_min=1, taken=(), **kw):
        return dict(market="breakout", pnl=pnl, peak_pnl=pnl if peak is None else peak,
                    opened_ts=time.time() - age_min * 60, tp_taken=list(taken), **kw)

    def test_clip(self):
        assert bot.plan_exit(self.pos(0.10), "clip", 0.20).action == "HOLD"
        assert "trailing stop 20%" in bot.plan_exit(self.pos(-0.21), "clip", 0.20).reason
        assert "trailing stop 10%" in bot.plan_exit(self.pos(0.25, peak=0.40), "clip", 0.20).reason   # после +30% — трейл 10%
        assert bot.plan_exit(self.pos(0.30, peak=0.40), "clip", 0.20).action == "HOLD"
        ed = bot.plan_exit(self.pos(-0.01, peak=0.05, age_min=11), "clip", 0.20)
        assert ed.action == "SELL" and "not up 10m" in ed.reason                 # не вырос за 10 мин — по цене входа
        assert bot.plan_exit(self.pos(0.04, peak=0.05, age_min=11), "clip", 0.20).action == "HOLD"
        ed = bot.plan_exit(self.pos(1.0), "clip", 0.20)
        assert ed.action == "SELL" and ed.fraction == 1.0                         # всё на 2×

    def test_ladder_shares_of_original_bag(self):
        ed = bot.plan_exit(self.pos(0.5), "ladder", 0.20)
        assert ed.rung == 0 and ed.fraction == pytest.approx(0.33)
        ed = bot.plan_exit(self.pos(1.0, taken=[0]), "ladder", 0.20)
        assert ed.rung == 1 and ed.fraction == pytest.approx(0.33 / 0.67, abs=1e-3)   # треть исходной = половина остатка
        assert bot.plan_exit(self.pos(1.5, taken=[0, 1]), "ladder", 0.20).action == "HOLD"   # остаток — на трейлинге

    def test_scale_out_and_doubling(self):
        ed = bot.plan_exit(self.pos(0.21), "scaleOut", 0.20)
        assert ed.rung == 0 and ed.fraction == pytest.approx(0.10)
        assert bot.plan_exit(self.pos(0.25, taken=[0]), "scaleOut", 0.20).action == "HOLD"   # следующая ступень +32%
        assert bot.plan_exit(self.pos(0.33, taken=[0]), "scaleOut", 0.20).rung == 1
        assert bot._plan_tp(bot.EXIT_PLANS["doubling"], 1)[0] == pytest.approx(3.0)           # 2× → 4×
        assert bot._plan_tp(bot.EXIT_PLANS["doubling"], 3) is None                            # три ступени продали всё

    def test_trailing_tp_stall_steplock_tighten(self):
        assert bot.plan_exit(self.pos(1.05, peak=1.10), "trailingTp", 0.30).action == "HOLD"
        ed = bot.plan_exit(self.pos(0.75, peak=1.10), "trailingTp", 0.30)
        assert ed.rung == 0 and ed.fraction == 0.5                                # половина на откате 15% после 2×
        now = time.time()
        assert bot.plan_exit(self.pos(0.18, peak=0.20, peak_ts=now - 200), "stall", 0.30).action == "HOLD"
        assert "no new high" in bot.plan_exit(self.pos(0.18, peak=0.20, peak_ts=now - 301), "stall", 0.30).reason
        assert "step lock holds +25%" in bot.plan_exit(self.pos(0.24, peak=0.55), "stepLock", 0.49).reason
        assert bot.plan_exit(self.pos(0.30, peak=0.55), "stepLock", 0.49).action == "HOLD"
        assert bot.plan_exit(self.pos(0.40, peak=0.50, age_min=5), "tighten", 0.20).action == "HOLD"
        assert "15%" in bot.plan_exit(self.pos(0.27, peak=0.50, age_min=11), "tighten", 0.20).reason
        assert "8%" in bot.plan_exit(self.pos(0.37, peak=0.50, age_min=31), "tighten", 0.20).reason

    def test_all_plans_defined(self):
        assert set(bot.EXIT_PLANS) == {"clip", "stake", "flip", "ladder", "trailingTp", "trail", "scaleOut", "scalp",
                                       "take30", "stall", "stepLock", "tighten", "doubling"}
        for pid in bot.EXIT_PLANS:
            assert bot.plan_exit(self.pos(0.01), pid, 0.20).action == "HOLD"

    def test_decide_exit_flow_exits(self):
        rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[(0.6, 0.4)])
        assert bot.decide_exit(self.pos(0.05), None, rc).action == "HOLD"
        assert "no-trade" in bot.decide_exit(self.pos(0.05, flow_drain=dict(idle_s=301, drop_pct=0, sells_over_buys=False)), None, rc).reason
        assert "drain" in bot.decide_exit(self.pos(-0.05, flow_drain=dict(idle_s=3, drop_pct=12, sells_over_buys=True)), None, rc).reason
        assert bot.decide_exit(self.pos(-0.05, flow_drain=dict(idle_s=3, drop_pct=12, sells_over_buys=False)), None, rc).action == "HOLD"
        assert "shock" in bot.decide_exit(self.pos(-0.05, flow_shock=(time.time(), 0.2, 0.1)), None, rc).reason
        assert bot.decide_exit(self.pos(0.70), None, rc).action == "HOLD"          # лестница пресета не действует


def test_breakout_profile_and_strategy(tmp_path, monkeypatch):
    mk = appmod._markets()["breakout"]
    assert mk["breakout"]["minutes"] == 10 and mk["active"]["buckets"] == 9 and mk["delay_s"] == 0.0
    assert mk["min_mcap"] == appmod._markets()["mid"]["min_mcap"]
    assert strategy.get("breakout_v1")["market"] == "breakout" and "breakout" in bot.PAPER_ONLY_MARKETS
    from fastapi import HTTPException
    from test_app import _mu_client
    c = _mu_client(tmp_path, monkeypatch)
    pk = "Usr6666666666666666666666666666666666666666"
    assert c.post("/api/strategy/select", json={"id": "breakout_v1"}, headers={"X-Wallet": pk}).status_code == 200
    sess = appmod.get_session(pk)
    assert appmod._bot_market(sess) == "breakout"
    card = next(s for s in c.get("/api/strategies?view=public", headers={"X-Wallet": pk}).json()["strategies"] if s["id"] == "breakout_v1")
    assert card["exit_note"] and card["exit_label"]
    monkeypatch.setattr(sess, "mode", "LIVE")
    with pytest.raises(HTTPException) as e:
        appmod._bot_buy_fn(sess)("sol", MINT, 0.5)
    assert e.value.status_code == 409 and "paper-only" in e.value.detail


def test_breakout_scan_gate(tmp_path, monkeypatch):
    """Скан рынка breakout пропускает токен только на пробое тихого диапазона при живой торговле."""
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    quiet = [4e-6 * k for k in (1.00, 1.02, 1.01, 1.03, 1.00, 1.02, 1.01, 1.02, 1.03, 1.01, 1.02)]
    f = _flow(quiet, 4e-6 * 1.05)
    f._started_at = time.time() - 7200
    mf = f._m[MINT]
    mf.symbol, mf.created_ts, mf.complete = "BRK", time.time() - 7200, True
    mf.pool_px["P"] = (mf.price_sol, int(300e9), int(150e15), time.time())
    mf.buckets.append([time.time() - 30, 30, 5, int(12e9), int(2e9)])
    monkeypatch.setattr(pumpflow, "FLOW", f)
    monkeypatch.setattr(appmod.pumpflow, "FLOW", f)
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 150.0)
    base = dict(price=mf.price_sol * 150, dex_price=mf.price_sol * 150, mcap=630_000, liq=90_000, age_h=2.0, vol_h1=5000, sym="BRK", dex="pumpswap")
    monkeypatch.setattr(appmod, "_mid_dex", lambda ms: {m: base for m in ms})
    monkeypatch.setattr(appmod, "_mid_market", lambda m, dexd, rate: dict(base))
    monkeypatch.setattr(appmod.rugmodel, "score", lambda d, snap: None)
    monkeypatch.setattr(appmod, "_hotbot_rules", lambda *a, **k: None)
    monkeypatch.setattr(appmod.zooai, "enabled", lambda: False)
    monkeypatch.setattr(appmod.agents.TEAM, "request", lambda *a, **k: None)
    monkeypatch.setattr(appmod.agents.TEAM, "verdict", lambda m: None)
    monkeypatch.setattr(appmod.agents.TEAM, "enabled", lambda: False)
    monkeypatch.setattr(f, "entry_snapshot", lambda m: {"5m": {"b": 30}})
    monkeypatch.setattr(f, "metrics", lambda m, w=300: dict(symbol="BRK", buy_ratio=0.8, unique_buyers=20, full=True))
    s = appmod.ST
    monkeypatch.setattr(s, "positions", [])
    out = appmod.screen_market("sol", s, "breakout")
    assert [d["decision"]["address"] for d in out["decisions"]] == [MINT]
    feats = out["decisions"][0]["decision"]["features"]
    assert feats["market"] == "breakout" and feats["brk_buyers"] == 4 and feats["brk_range"] <= 10
    mf.price_sol = 4e-6 * 1.02                                                     # цена вернулась в диапазон — входа нет
    assert appmod.screen_market("sol", s, "breakout")["decisions"] == []
    mf.price_sol = 4e-6 * 1.20                                                     # улетело на +16% выше пробоя — не догоняем
    assert appmod.screen_market("sol", s, "breakout")["decisions"] == []


def test_smooth_climb_and_young_top10_rules():
    """11.11 щит HOTBOT: разгон «по линейке» (+50% за 30 мин без отката) и топ-10 ≥25% у токена моложе 2 ч."""
    f = pumpflow.PumpFlow()
    now = time.time()
    mf = f._get(MINT, now)
    cur = int(now // 60 * 60)
    px = 1.0
    for i in range(30):                              # +1.5% каждую минуту, ровно
        mf.candles.append([cur - (30 - i) * 60, px, px * 1.015, px, px * 1.015, 0.0])
        px *= 1.015
    mf.price_sol = px
    cl = f.climb(MINT)
    assert cl["rise_pct"] >= 50 and cl["step_vol"] < 0.005 and cl["off_high_pct"] < 1
    assert appmod._hotbot_rules({}, dict(climb=cl)).startswith("smooth_climb")
    assert appmod._hotbot_rules({}, dict(climb=dict(cl, step_vol=0.06))) is None       # рваный рост — нормальный рынок
    assert appmod._hotbot_rules({}, dict(climb=dict(cl, off_high_pct=12))) is None     # уже откатил от максимума
    assert appmod._hotbot_rules({}, dict(climb=dict(cl, rise_pct=30))) is None
    g = pumpflow.PumpFlow()
    g._get(MINT, now).price_sol = 1.0
    assert g.climb(MINT) is None                                                       # свечей нет — не судим
    assert appmod._hotbot_rules({}, dict(top10_supply_pct=30, age_s=3600)).startswith("top10_young")
    assert appmod._hotbot_rules({}, dict(top10_supply_pct=30, age_s=3 * 3600)) is None  # старше 2 ч — не судим
    assert appmod._hotbot_rules({}, dict(top10_supply_pct=20, age_s=3600)) is None
