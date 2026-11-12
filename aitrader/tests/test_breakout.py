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


# ── 11.11: стоп с учётом издержек, выбор плана, слоёный запуск, балльная оценка ──
def test_stop_accounts_for_exit_costs():
    rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[])
    base = dict(market="mid", peak_pnl=0.0, opened_ts=time.time() - 60, tp_taken=[])
    sim = dict(fee=0.003, impact=0.004)                       # издержки выхода ≈ 0.7% + 5% проскальзывания стопа
    assert bot.stop_cost(dict(sim_entry=sim)) == pytest.approx(0.057)
    assert bot.decide_exit(dict(base, pnl=-0.15, sim_entry=sim), None, rc).reason.startswith("Mid stop")   # раньше ждал −20%
    assert bot.decide_exit(dict(base, pnl=-0.13, sim_entry=sim), None, rc).action == "HOLD"
    assert bot.decide_exit(dict(base, pnl=-0.15), None, rc).action == "HOLD"                               # LIVE/без расчёта — как было
    assert bot.decide_exit(dict(base, pnl=-0.15, sim_entry=sim), None, rc, dict(bot.CFG, stop_net=0)).action == "HOLD"
    pos = dict(market="breakout", pnl=-0.16, peak_pnl=0.0, opened_ts=time.time() - 60, tp_taken=[], sim_entry=sim)
    assert "trailing stop 20%" in bot.decide_exit(pos, None, rc).reason
    floor = dict(pos, pnl=0.03, peak_pnl=0.05, opened_ts=time.time() - 700)
    assert bot.decide_exit(floor, None, rc).action == "HOLD"                                               # «по цене входа» — без поправки


def test_plan_picker_api(tmp_path, monkeypatch):
    from test_app import _mu_client
    c = _mu_client(tmp_path, monkeypatch)
    pk, H = "Usr7777777777777777777777777777777777777777", {"X-Wallet": "Usr7777777777777777777777777777777777777777"}
    c.post("/api/strategy/select", json={"id": "breakout_v1"}, headers=H)
    card = next(s for s in c.get("/api/strategies?view=public", headers=H).json()["strategies"] if s["id"] == "breakout_v1")
    assert len(card["exit_plans"]) == 13 and card["plan"] == "clip" and card["trail"] == 0.20 and all(p["summary"] for p in card["exit_plans"])
    assert c.post("/api/bot/config", json={"brk_plan": "scalp", "brk_trail": 0.15}, headers=H).status_code == 200
    assert c.post("/api/bot/config", json={"brk_plan": "nope"}, headers=H).status_code == 400
    assert c.post("/api/bot/config", json={"brk_trail": 0.9}, headers=H).status_code == 400
    card = next(s for s in c.get("/api/strategies?view=public", headers=H).json()["strategies"] if s["id"] == "breakout_v1")
    assert card["plan"] == "scalp" and card["trail"] == 0.15
    cfg = appmod.get_session(pk).bot.cfg
    pos = dict(market="breakout", pnl=0.21, peak_pnl=0.21, opened_ts=time.time() - 60, tp_taken=[])
    ed = bot.decide_exit(pos, None, {}, cfg)
    assert ed.action == "SELL" and ed.fraction == 0.5 and "Quick scalp" in ed.reason                        # план из настроек юзера
    other = next(s for s in c.get("/api/strategies?view=public", headers=H).json()["strategies"] if s["id"] == "migration_v1")
    assert "exit_plans" not in other


def test_layered_launch_check():
    import layered
    sup = 10 ** 15
    w = [("dev", 40 * 10 ** 12, True), ("b1", 20 * 10 ** 12, False), ("b2", 20 * 10 ** 12, False), ("b3", 5 * 10 ** 12, False)]
    held = {"dev": 40 * 10 ** 12, "b1": 20 * 10 ** 12, "b2": 20 * 10 ** 12, "b3": 5 * 10 ** 12}
    assert layered.verdict_of(w, held, sup)["layered"] is False                       # все держат купленное
    v = layered.verdict_of(w, dict(held, b1=0, b2=10 ** 12), sup)
    assert v["layered"] and v["moved_wallets"] == 2                                   # двое увели без продажи
    assert layered.verdict_of(w, dict(held, dev=20 * 10 ** 12), sup)["layered"]       # дев увёл 2% саплая
    assert layered.verdict_of(w, dict(held, b3=0), sup)["layered"] is False           # один мелкий — не слоёный
    assert layered.verdict_of(w, {}, sup) == dict(layered=False, moved_wallets=0, moved_pct=0, dev_moved_pct=0, checked=0)
    calls = []
    def rpc(method, params):
        calls.append(params[0])
        amt = {"dev": 0}.get(params[0], held[params[0]])
        return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}}]}
    ch = layered.LayeredCheck(rpc=rpc)
    ch.request(MINT, w, sup)
    assert ch.pending(MINT) and ch.verdict(MINT) is None
    assert ch.tick() == 1 and calls == ["dev", "b1", "b2", "b3"]
    assert ch.verdict(MINT)["layered"] and ch.verdict(MINT)["dev_moved_pct"] == 4.0 and not ch.pending(MINT)
    ch.request(MINT, w, sup)
    assert ch.tick() == 0                                                             # ответ в кэше — RPC не дёргаем
    bad = layered.LayeredCheck(rpc=lambda m, p: (_ for _ in ()).throw(RuntimeError("429")))
    bad.request(MINT, w, sup); bad.tick()
    assert bad.verdict(MINT) is None and not bad.pending(MINT)                        # сеть упала — не судим и не ждём


def test_launch_wallets_from_flow():
    f = pumpflow.PumpFlow()
    mf = f._get(MINT, time.time())
    assert f.launch_wallets(MINT) == ([], 0)                                          # создание не видели — учёт неполный
    mf.create_slot, mf.creator = 100, "dev"
    mf.slot_buyers = {100: {"b1", "b2"}, 101: {"b3"}}
    mf.bal = {"dev": 50, "b1": 30, "b2": 0, "b3": 40, "late": 999}
    rows, sup = f.launch_wallets(MINT)
    assert rows == [("dev", 50, True), ("b3", 40, False), ("b1", 30, False)] and sup > 0


def test_fit_score_shadow():
    clean = dict(dev_supply_pct=0, launch_bundle_pct=0, sniper_supply_pct=0, top10_supply_pct=15, crash_1m_max_pct=5,
                 dd_from_high_pct=10, holders_obs=500, lp_mc_ratio=0.15, **{"5m": dict(uniq=40, br=0.85)})
    assert appmod._fit_score(clean) == 100.0
    edge = dict(dev_supply_pct=10, launch_bundle_pct=10, sniper_supply_pct=10, top10_supply_pct=40, crash_1m_max_pct=20,
                dd_from_high_pct=50, holders_obs=100, lp_mc_ratio=0.05, **{"5m": dict(uniq=12, br=0.68)})
    assert appmod._fit_score(edge) == 0.0
    assert 40 < appmod._fit_score(dict(clean, dev_supply_pct=10, crash_1m_max_pct=20, top10_supply_pct=40, holders_obs=100, lp_mc_ratio=0.05)) < 60
    assert appmod._fit_score(dict(dev_supply_pct=3)) is None                          # мало признаков — не оцениваем
    assert appmod.FIT_MIN == 0                                                        # по умолчанию входа не режет


def test_breakout_filter_overrides_leave_mid_untouched(monkeypatch):
    """12.11: мягче только у Breakout — рост за час, скачок на запуске у старых токенов, модель рагов."""
    import app as appmod
    monkeypatch.setitem(appmod.HOT, "launch_x10s_max", 4.0)
    monkeypatch.setitem(appmod.HOT, "chg_h1_max", 500.0)
    over = appmod._markets()["breakout"]["hot"]
    assert "hot" not in appmod._markets()["mid"] and not appmod._markets()["mid"].get("rug_thr_add")
    assert appmod._markets()["breakout"]["rug_thr_add"] > 0
    assert appmod._hotbot_rules(dict(chg_h1=900), {}).startswith("chg_h1")
    assert appmod._hotbot_rules(dict(chg_h1=900), {}, over=over) is None
    old, young = dict(launch_x10s=6, age_s=5 * 3600), dict(launch_x10s=6, age_s=3600)
    assert appmod._hotbot_rules({}, old).startswith("launch_x10s")            # основной бот режет любой возраст
    assert appmod._hotbot_rules({}, old, over=over) is None                   # Breakout: старше 4 ч — до 20×
    assert appmod._hotbot_rules({}, young, over=over) is None                 # 12.11: у Breakout только экстремальный 20×
    assert appmod._hotbot_rules({}, dict(launch_x10s=25, age_s=3600), over=over).startswith("launch_x10s")
    assert appmod._hotbot_rules({}, dict(launch_x10s=25, age_s=5 * 3600), over=over).startswith("launch_x10s")


def test_breakout_takes_old_tokens():
    """13.11: Breakout смотрит и токены старше суток (рынок HOTBOT); основной бот — прежний возраст."""
    import app as appmod
    mk = appmod._markets()
    assert mk["breakout"]["max_age_h"] >= 720 and mk["breakout"]["max_mcap"] >= 5_000_000
    assert mk["mid"]["max_age_h"] == appmod.MID_MAX_AGE_H
    old = {"age_s": 3 * 86400, "5m": dict(uniq=30, br=0.8, net=5.0, b=20)}
    assert (appmod._hotbot_rules({}, old) or "").startswith("age")
    assert appmod._hotbot_rules({}, old, over=mk["breakout"]["hot"]) is None
