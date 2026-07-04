"""Тесты офлайн review-loop: атрибуция исходов (топливо), эдж кошельков/KOL,
предложения по конфигу, edge-weighting в scoring и house-эндпоинты (owner-gated).
Всё на чистых функциях/内存 + tmp-изоляция落盘; сеть/gmgn-cli не трогаем."""
import datetime
import json

import pytest

import app as appmod
import review
import wallets


# ── помощники ────────────────────────────────────────────────────────────────
def sell(pnl, tracked=(), *, priority=50, age=30, buy_ratio=0.6, sm=2,
         fraction=1.0, size=0.1):
    """SELL-запись журнала с атрибуцией входа (как её кладёт do_sell)."""
    return dict(action="SELL", symbol="X", reason="x", pnl=pnl, size_sol=size,
                fraction=fraction, attrib=dict(tracked=list(tracked), priority=priority,
                age_min=age, buy_ratio=buy_ratio, sm_confluence=sm))


def feat(**over):
    base = dict(
        address="ADDR" + "x" * 40, symbol_raw="CLEAN", symbol_safe="CLEAN",
        price=0.001, mcap=180_000, vol_1h=900_000, age_min=42, chg_1h=0.35,
        chg_5m=0.10, buys=600, sells=400, swaps=1000, liquidity=54_000,
        buy_ratio=0.6, turnover=5.0, honeypot=False, renounced_mint=True,
        renounced_freeze=True, burn_ratio=0.0, buy_tax=0.0, sell_tax=0.0,
        rug_ratio=0.0, bundler=0.04, dev_hold=0.03, top10=0.22,
        smart_degen=2, renowned=1, sniper_count=0, sm_confluence=3)
    base.update(over)
    return appmod.TokenFeatures(**base)


# ── разбор журнала: только полные закрытия с pnl ────────────────────────────
class TestClosedTrades:
    def test_filters_full_sells_with_attrib(self):
        recs = [sell(0.5, ["W"]),
                sell(0.3, ["W"], fraction=0.4),          # частичный TP — не считаем реализацией
                dict(action="SCREEN", symbol="Y"),        # не SELL
                dict(action="SELL", symbol="Z", reason="no pnl")]  # без pnl
        out = review.closed_trades(recs)
        assert len(out) == 1 and out[0]["pnl"] == 0.5
        assert out[0]["attrib"]["tracked"] == ["W"]

    def test_missing_fraction_treated_full(self):
        # старые ручные SELL без fraction = полное закрытие
        assert len(review.closed_trades([{"action": "SELL", "pnl": 0.2}])) == 1


# ── эдж кошельков: нейтрально на малой выборке, взвешено на достаточной ──────
class TestWalletEdge:
    def test_neutral_below_min_trades(self):
        e = review.wallet_edge([sell(0.5, ["W"]) for _ in range(3)], min_trades=5)
        assert e["W"]["weight"] == 1.0 and e["W"]["sample_ok"] is False

    def test_winner_gets_weight_above_one(self):
        e = review.wallet_edge([sell(0.5, ["W"]) for _ in range(6)])
        assert e["W"]["sample_ok"] is True and e["W"]["weight"] > 1.0

    def test_loser_gets_weight_below_one(self):
        e = review.wallet_edge([sell(-0.35, ["L"]) for _ in range(6)])
        assert e["L"]["weight"] < 1.0 and e["L"]["expectancy_R"] < 0

    def test_attribution_splits_per_wallet(self):
        recs = [sell(0.6, ["A", "B"]) for _ in range(6)] + [sell(-0.35, ["B"]) for _ in range(6)]
        e = review.wallet_edge(recs)
        assert e["A"]["trades"] == 6 and e["B"]["trades"] == 12      # B был в обеих группах


# ── предложения по конфигу ───────────────────────────────────────────────────
class TestPropose:
    def test_silent_below_min_trades(self):
        assert review.propose([sell(-0.3, priority=40) for _ in range(3)]) == []

    def test_raises_min_priority_when_low_priority_loses(self):
        recs = ([sell(-0.35, priority=40) for _ in range(6)]
                + [sell(0.6, priority=85) for _ in range(6)])
        props = review.propose(recs, cfg={"hard_stop_pct": 0.35})
        mp = next((p for p in props if p["param"] == "min_priority"), None)
        assert mp and mp["target"] == "bot" and mp["suggested"] == 70
        assert mp["applicable"] is True and "evidence" in mp

    def test_no_proposal_when_already_set(self):
        recs = ([sell(-0.35, priority=40) for _ in range(6)]
                + [sell(0.6, priority=85) for _ in range(6)])
        # min_priority уже 70 у бота → предложение не дублируем
        props = review.propose(recs, cfg={"hard_stop_pct": 0.35, "bot_min_priority": 70})
        assert not any(p["param"] == "min_priority" for p in props)


# ── обзор KOL ────────────────────────────────────────────────────────────────
class TestKolReview:
    def test_flags_unreliable_over_min_calls(self, monkeypatch):
        monkeypatch.setattr(review.kol, "rating", lambda: [
            dict(username="bad", calls=10, winrate=0.1, median_x=0.3),
            dict(username="good", calls=8, winrate=0.6, median_x=2.1),
            dict(username="few", calls=2, winrate=0.0, median_x=0.0)])
        out = {r["username"]: r for r in review.kol_review(min_calls=4)}
        assert set(out) == {"bad", "good"}                # few отфильтрован по порогу коллов
        assert out["bad"]["verdict"] == "unreliable" and out["good"]["verdict"] == "reliable"


# ── edge-weighting в wallets + scoring ──────────────────────────────────────
class TestEdgeWeighting:
    @pytest.fixture(autouse=True)
    def _iso_edges(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wallets, "EDGES_PATH", tmp_path / "wallet_edges.json")
        monkeypatch.setattr(wallets, "_edges_mem", None)

    def test_edge_weight_roundtrip(self):
        wallets.save_edges({"W": dict(weight=1.6, trades=8)})
        assert wallets.edge_weight("W") == 1.6
        assert wallets.edge_weight("unknown") == 1.0      # неизвестен → нейтрально

    def test_neutral_weights_preserve_old_scoring(self):
        # без файла эджа веса = 1.0 → wsum == tracked_hits → как до фичи
        base = appmod.priority_score(feat(tracked_hits=0), 0.8, "early")
        boosted = appmod.priority_score(feat(tracked_hits=3), 0.8, "early")
        assert boosted > base

    def test_weak_wallet_scores_below_neutral(self):
        wallets.save_edges({"WEAK": dict(weight=0.3)})
        weak = appmod.priority_score(feat(tracked_hits=1, tracked_names=["WEAK"]), 0.8, "early")
        neutral = appmod.priority_score(feat(tracked_hits=1, tracked_names=["OK"]), 0.8, "early")
        assert weak < neutral                             # доказанно убыточный кошелёк режет приоритет


# ── интеграция: атрибуция входа доезжает до SELL-записи + house-эндпоинты ────
def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "PUBLIC_DEMO", False)
    monkeypatch.setattr(appmod, "ADMIN_MODE", False)
    monkeypatch.setattr(appmod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "FILTERS_PATH", tmp_path / "filters.json")
    monkeypatch.setattr(appmod, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(appmod, "LOG_PATH", tmp_path / "trade_decisions.jsonl")
    monkeypatch.setattr(appmod, "REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(appmod, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(appmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(appmod, "CFG", dict(appmod.CFG))          # изоляция мутаций CFG
    monkeypatch.setattr(appmod, "_ATTRIB_CACHE", {})
    monkeypatch.setattr(wallets, "EDGES_PATH", tmp_path / "wallet_edges.json")
    monkeypatch.setattr(wallets, "_edges_mem", None)
    monkeypatch.setattr(appmod, "MK", appmod.MarketLayer())
    monkeypatch.setattr(appmod, "SESSIONS", {})
    monkeypatch.setattr(appmod, "ST", appmod.get_session(appmod.DEFAULT_PUBKEY))
    return TestClient(appmod.app)


class TestAttributionFlow:
    ADDR = "CLEANCATxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        return _client(tmp_path, monkeypatch)

    def test_sell_record_carries_entry_attribution(self, client):
        # скрин запомнил бы атрибуцию; здесь сеем кэш напрямую (эквивалент ACTION-снимка)
        appmod._ATTRIB_CACHE[self.ADDR] = dict(tracked=["Whale"], priority=71,
                                               age_min=8.0, buy_ratio=0.62, sm_confluence=3)
        assert client.post("/api/buy", json={"address": self.ADDR, "size_sol": 0.1,
                                             "chain": "sol"}).status_code == 200
        assert client.post("/api/sell", json={"address": self.ADDR}).status_code == 200
        sells = [json.loads(x) for x in appmod.LOG_PATH.read_text().splitlines()
                 if '"SELL"' in x]
        assert sells and sells[-1]["attrib"]["tracked"] == ["Whale"]
        assert isinstance(sells[-1]["hold_min"], (int, float))     # opened_ts → время удержания


class TestReviewEndpoints:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        return _client(tmp_path, monkeypatch)

    def test_get_review_shape(self, client):
        d = client.get("/api/review").json()
        assert "report" in d and "edges" in d
        assert set(("wallet_edge", "kol_review", "proposals", "overall")) <= set(d["report"])

    def test_run_persists_edges_and_report(self, client):
        # накопим достаточную выборку по кошельку → появится вес + файл отчёта
        for _ in range(6):
            appmod.log("SELL", "X", "x", dict(pnl=0.5, size_sol=0.1, fraction=1.0,
                       attrib=dict(tracked=["W"], priority=80)), pubkey="local")
        d = client.post("/api/review/run").json()
        assert d["edges"]["W"]["weight"] > 1.0 and d["edges"]["W"]["trades"] == 6
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        assert (appmod.REVIEWS_DIR / f"{day}.json").exists()
        assert wallets.edge_weight("W") > 1.0                       # scoring теперь видит вес

    def test_run_owner_gated(self, client):
        assert client.post("/api/review/run", headers={"X-Wallet": "ExtPk"}).status_code == 403
        assert client.post("/api/review/run").status_code == 200    # локальная сессия = оператор

    def test_apply_filters_and_cfg_whitelist(self, client):
        r = client.post("/api/review/apply",
                        json={"target": "filters", "param": "max_age_min", "value": 45})
        assert r.status_code == 200 and r.json()["value"] == 45.0
        assert client.get("/api/filters").json()["filters"]["max_age_min"] == 45.0

        r = client.post("/api/review/apply",
                        json={"target": "CFG", "param": "buy_ratio_reject", "value": 0.5})
        assert r.status_code == 200 and appmod.CFG["buy_ratio_reject"] == 0.5

        # ключ вне белого списка → 400 (нельзя писать произвольный CFG)
        assert client.post("/api/review/apply",
                           json={"target": "CFG", "param": "equity_sol", "value": 999}).status_code == 400

    def test_apply_owner_gated(self, client):
        r = client.post("/api/review/apply", headers={"X-Wallet": "ExtPk"},
                        json={"target": "bot", "param": "min_priority", "value": 70})
        assert r.status_code == 403
