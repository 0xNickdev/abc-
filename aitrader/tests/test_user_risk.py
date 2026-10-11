"""Per-user риск (бета): у каждого тестера свои размер, лимиты, стоп, тейки, трейлинг.
House-сессия остаётся на глобальном CFG."""
import pytest

import app as appmod
import bot as botmod
from test_app import _mu_client

W1 = "RiskWalletOnexxxxxxxxxxxxxxxxxxxxxxxxxxxx"
W2 = "RiskWalletTwoxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _mu_client(tmp_path, monkeypatch)


def test_house_session_keeps_global_cfg(client):
    assert appmod.ST.risk_cfg() is appmod.CFG
    assert appmod.ST.risk_prefs is None


def test_defaults_come_from_strategy_and_standard_preset(client):
    s = appmod.get_session(W1)
    v = s.risk_view()
    pre = appmod.strategy.get(s.strategy_id)["preset"]
    assert v["stop_pct"] == pre["hard_stop_pct"]
    assert [(t["at_pct"], t["sell_pct"]) for t in v["take_profit"]] == [tuple(x) for x in pre["tp_ladder"]]
    assert v["size_sol"] == appmod.RISK_PRESETS["standard"]["size_sol"]
    assert v["max_positions"] == appmod.RISK_PRESETS["standard"]["max_positions"]


def test_strategy_switch_changes_exits(client):
    s = appmod.get_session(W1)
    s.set_strategy("abc_degen_v1")
    assert s.risk_cfg()["hard_stop_pct"] == appmod.strategy.get("abc_degen_v1")["preset"]["hard_stop_pct"]


def test_sessions_are_isolated(client):
    a, b = appmod.get_session(W1), appmod.get_session(W2)
    a.set_risk(preset="bold", overrides={"stop_pct": 0.1})
    assert b.risk_view()["stop_pct"] != 0.1
    assert b.risk_view()["size_sol"] == appmod.RISK_PRESETS["standard"]["size_sol"]
    assert a.risk_cfg()["hard_stop_pct"] == 0.1


def test_sanitize_clamps_and_orders_take_profit(client):
    out = appmod.sanitize_risk(dict(
        stop_pct=5, trailing_pct=0, size_sol=99, max_positions=0, pause_after_losses=-3,
        take_profit=[dict(at_pct=3, sell_pct=0.8), dict(at_pct=0.5, sell_pct=0.5), dict(at_pct=9, sell_pct=0.5)],
        bogus=1))
    assert out["stop_pct"] == 0.6 and out["trailing_pct"] == 0.05
    assert out["size_sol"] == appmod.CFG["bot_max_per_trade_sol"]
    assert out["max_positions"] == 1 and out["pause_after_losses"] == 0
    assert "bogus" not in out
    tp = out["take_profit"]
    assert [t["at_pct"] for t in tp] == [0.5, 3]          # по возрастанию, 3-я ступень не влезла
    assert round(sum(t["sell_pct"] for t in tp), 4) == 1.0  # не больше 100% позиции


def test_preset_resets_money_overrides_but_keeps_exit_overrides(client):
    s = appmod.get_session(W1)
    s.set_risk(overrides={"size_sol": 0.02, "stop_pct": 0.15})
    s.set_risk(preset="careful")
    v = s.risk_view()
    assert v["size_sol"] == appmod.RISK_PRESETS["careful"]["size_sol"]
    assert v["stop_pct"] == 0.15


def test_override_none_restores_default(client):
    s = appmod.get_session(W1)
    s.set_risk(overrides={"stop_pct": 0.15})
    s.set_risk(overrides={"stop_pct": None})
    assert s.risk_view()["stop_pct"] == appmod.strategy.get(s.strategy_id)["preset"]["hard_stop_pct"]


def test_prefs_persist_across_reload(client):
    s = appmod.get_session(W1)
    s.set_risk(preset="bold", overrides={"trailing_pct": 0.3})
    again = appmod.load_user_risk(W1)
    assert again["preset"] == "bold" and again["overrides"]["trailing_pct"] == 0.3


def test_pause_after_losses_zero_means_never(client):
    s = appmod.get_session(W1)
    s.set_risk(overrides={"pause_after_losses": 0})
    s.risk.consec_losses = 50
    assert s.risk.fuse_reason() is None


def test_user_limits_apply_on_paper(client):
    s = appmod.get_session(W1)
    s.mode = "SHADOW"
    s.set_risk(overrides={"max_positions": 1})
    allow, why = s.risk.gate(0.01, 1, 0.0, fuses=appmod._fuses_on(s))
    assert not allow and "max concurrent" in why


def test_exit_rules_follow_user_stop(client):
    s = appmod.get_session(W1)
    s.set_risk(overrides={"stop_pct": 0.10})
    pos = dict(pnl=-0.12, peak_pnl=0.0, tp_taken=[])
    assert botmod.decide_exit(pos, None, s.risk_cfg(), s.bot.cfg).action == "SELL"
    assert botmod.decide_exit(pos, None, appmod.CFG, s.bot.cfg).action == "HOLD"   # house −25%


def test_risk_api_roundtrip(client):
    h = {"X-Wallet": W1}
    r = client.get("/api/risk", headers=h).json()
    assert r["editable"] and r["preset"] == "standard"
    r = client.post("/api/risk", headers=h, json={"preset": "careful", "overrides": {"stop_pct": 0.2}}).json()
    assert r["preset"] == "careful" and r["effective"]["stop_pct"] == 0.2
    assert client.post("/api/risk", headers=h, json={"preset": "yolo"}).status_code == 400
    r = client.post("/api/risk", headers=h, json={"reset": True}).json()
    assert r["overrides"] == {}


def test_house_risk_not_editable_via_api(client):
    assert client.post("/api/risk", json={"preset": "bold"}).status_code == 400


def test_trades_are_tagged_and_results_split_by_strategy(client, monkeypatch):
    import datetime
    addr = "CLEANCATxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    h = {"X-Wallet": W1}
    s = appmod.get_session(W1)
    s.set_strategy("abc_sniper_v1")
    s.set_risk(preset="careful")
    assert client.post("/api/buy", headers=h, json={"address": addr, "size_sol": 0.1, "chain": "sol"}).status_code == 200
    pos = s.positions[0]
    assert (pos["strategy"], pos["risk_preset"], pos["by"]) == ("abc_sniper_v1", "careful", "manual")
    assert client.post("/api/sell", headers=h, json={"address": addr}).status_code == 200
    m = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    cal = client.get("/api/pnl/calendar", headers=h, params={"scope": "me", "month": m}).json()
    assert cal["by_strategy"]["abc_sniper_v1"]["trades"] == 1
    assert cal["strategy_names"]["abc_sniper_v1"] == "Sniper"
    day = next(iter(cal["days"].values()))
    assert day["list"][0]["strategy"] == "abc_sniper_v1"
