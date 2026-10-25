"""Модель издержек бумажной торговли: вход дороже рынка, выход дешевле, фикс-издержки (priority/base
fee, рента ATA), стопы проскальзывают сильнее; календарь не вычитает процент повторно."""
import datetime
import time

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
    assert -0.06 < net < -0.01 and cost > 0                 # «в ноль по цене» = минус на издержках (рента вернулась)


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


def test_full_close_refunds_ata_rent(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    p = dict(address="AMMTOKEN", entry_liq=200_000.0, sim_fixed_sol=0.00229)
    monkeypatch.setattr(appmod, "SIM_RENT_REFUND", False)
    n_keep, c_keep = appmod.sim_exit(dict(p), 0.0, 1.0, 0.1, "Trailing")
    monkeypatch.setattr(appmod, "SIM_RENT_REFUND", True)
    n_back, c_back = appmod.sim_exit(dict(p), 0.0, 1.0, 0.1, "Trailing")
    refund = appmod.SIM_ATA_RENT_SOL - appmod.SIM_CLOSE_TX_FEE_SOL
    assert c_keep - c_back == pytest.approx(refund, abs=1e-6)
    assert n_back - n_keep == pytest.approx(refund / 0.1, abs=1e-4)
    n_part, _ = appmod.sim_exit(dict(p), 0.0, 0.4, 0.04, "TP1")       # частичная — ренты ещё нет
    monkeypatch.setattr(appmod, "SIM_RENT_REFUND", False)
    assert n_part == appmod.sim_exit(dict(p), 0.0, 0.4, 0.04, "TP1")[0]


def test_entry_fixed_costs_charged_once_across_partial_sells(tmp_path, monkeypatch):
    """Аудит 11.10: доля фикс-издержек входа списывалась на каждой частичной продаже, а потом
    на финале целиком ещё раз (×1.7 на позицию из трёх частей)."""
    _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(appmod, "SIM_RENT_REFUND", False)
    s = appmod.ST
    s.positions.append(dict(symbol="T", address="AMMTOKEN", size_sol=0.1, pnl=0.0, entry={}, chain="sol",
                            entry_price=1.0, cur_price=1.0, entry_liq=200_000.0, opened_ts=0,
                            sim_entry=dict(fixed_sol=0.00229), sim_fixed_sol=0.00229))
    fixed_seen = []
    real_exit = appmod.sim_exit
    def spy(p, *a, **k):
        fixed_seen.append(float(p.get("sim_fixed_sol", 0.0)) * a[1])
        return real_exit(p, *a, **k)
    monkeypatch.setattr(appmod, "sim_exit", spy)
    appmod.do_sell("AMMTOKEN", 0.4, "TP1", s)
    appmod.do_sell("AMMTOKEN", 0.5, "TP2", s)
    appmod.do_sell("AMMTOKEN", 1.0, "Trailing", s)
    assert sum(fixed_seen) == pytest.approx(0.00229, abs=1e-8)


def test_reclaim_endpoints_need_wallet_and_signature(tmp_path, monkeypatch):
    c = _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod.execution, "empty_token_accounts", lambda o, m=None: [dict(lamports=2039280)] * 3)
    monkeypatch.setattr(appmod.execution, "build_close_empty", lambda o, m=None: dict(tx="AAA", count=3, reclaim_sol=0.006118, more=0))
    assert c.get("/api/wallet/rent").json()["accounts"] == 0                 # без кошелька — нечего
    h = {"X-Wallet": "RentWalletxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
    r = c.get("/api/wallet/rent", headers=h).json()
    assert r == dict(accounts=3, reclaim_sol=0.006118)
    assert c.post("/api/tx/reclaim").status_code == 400


# ── 25.10: мид-покупки передают пул/площадку; якорь потока хранит издержки входа ──
def test_venue_fee_by_dex_id():
    a = "Tok1111111111111111111111111111111111111pump"
    assert appmod._sim_venue_fee(a, 10_000, "pumpswap") == appmod.SIM_FEE_AMM     # без цены — плоская AMM
    assert appmod._sim_venue_fee(a, 10_000, "raydium", 0.001) == appmod.SIM_FEE_AMM
    assert appmod._sim_venue_fee(a, 10_000, "pumpfun") == appmod.SIM_FEE_CURVE
    assert appmod._sim_venue_fee(a, 10_000) == appmod.SIM_FEE_CURVE              # без dexId — старое правило


def test_sim_entry_uses_pool(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 200.0)
    a = "Tok1111111111111111111111111111111111111pump"
    eff, _, sim = appmod.sim_entry(a, 1.0, 0.3, 40_000, "raydium")
    # 0.3% площадка + 2*0.3*200/40000=0.3% сдвиг + 1.5% проскальзывание
    assert sim["fee"] == 0.003 and sim["impact"] == 0.003 and abs(eff - 1.021) < 1e-9
    assert sim["liq"] == 40_000 and sim["dex"] == "raydium"


def test_mid_buy_passes_dex_pool(tmp_path, monkeypatch):
    from test_app import _mu_client
    _mu_client(tmp_path, monkeypatch)
    s = appmod.ST
    monkeypatch.setattr(s, "positions", [])
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 200.0)
    monkeypatch.setattr(appmod.dexadapter, "spot_pair",
                        lambda a: dict(baseToken=dict(symbol="M"), priceUsd="0.002", dexId="pumpswap",
                                       liquidity=dict(usd=40000)))
    monkeypatch.setattr(appmod.dexadapter, "spot_price", lambda a: 0.002)
    appmod.do_buy("sol", "MidTok2222pump", 0.3, s, market="mid")
    p = next(p for p in s.positions if p["address"] == "MidTok2222pump")
    # $0.002 × 1 млрд / $200 = 10 000 SOL капы → ступень PumpSwap 0.95%
    assert p["sim_entry"]["fee"] == 0.0095 and p["sim_entry"]["impact"] < 0.01
    assert p["entry_liq"] == 40000


def test_flow_anchor_keeps_entry_costs(monkeypatch):
    import threading, types
    import bot, pumpflow, watcher as w
    MINT = "Anc1111111111111111111111111111111111111pump"
    p = dict(address=MINT, entry_price=0.00204, chain="sol", market="mid", auto=True,
             opened_ts=time.time() - 5, sim_entry=dict(mid=0.002))           # заплатили 2% на входе
    sess = types.SimpleNamespace(positions=[p], lock=threading.Lock(),
                                 bot=types.SimpleNamespace(enabled=True, cfg=dict(bot.CFG)))
    monkeypatch.setattr(w.geyser.FEED, "set_mints", lambda m: None)
    monkeypatch.setattr(w.geyser.FEED, "prices_usd", lambda m: {})
    monkeypatch.setattr(w.pumpcurve, "usd_prices", lambda m: {})
    monkeypatch.setattr(w.dexadapter, "spot_prices", lambda m: {MINT: 0.002})
    f = pumpflow.PumpFlow()
    f._get(MINT, time.time()).price_sol = 1e-5
    monkeypatch.setattr(pumpflow, "FLOW", f)
    w.PositionWatcher(lambda: [sess], dict(hard_stop_pct=0.5), lambda s_: (lambda *a, **k: None)).poll_once()
    assert p["pnl"] == pytest.approx(0.002 / 0.00204 - 1, abs=1e-4)          # ≈ −2%, а не 0


def test_pumpswap_fee_tiers_by_mcap_in_sol(monkeypatch):
    monkeypatch.setattr(appmod.pumpcurve, "sol_usd", lambda: 100.0)
    a = "Tok1111111111111111111111111111111111111pump"
    # $100k капы при SOL=$100 → 1000 SOL → 1.20%; $1M → 10 000 SOL → 0.95%; $300k → 3000 SOL → 1.10%
    assert appmod._sim_venue_fee(a, 30_000, "pumpswap", 100_000 / 1e9) == 0.0120
    assert appmod._sim_venue_fee(a, 30_000, "pumpswap", 1_000_000 / 1e9) == 0.0095
    assert appmod._sim_venue_fee(a, 30_000, "pumpswap", 300_000 / 1e9) == 0.0110
    p = dict(address=a, entry_liq=30_000, sim_entry=dict(fee=0.011, dex="pumpswap"), sim_fixed_sol=0.0)
    net, _ = appmod.sim_exit(p, 0.0, 1.0, 0.3, "x")            # выход — та же ступень 1.1%
    assert net < -0.011
