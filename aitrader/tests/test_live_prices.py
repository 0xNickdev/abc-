"""Live-цены скринера: /api/prices + фоновая выжимка DexScreener/кривой.
Контур стопов (spot_prices) не трогаем — отдельная функция spot_quotes."""
import types

import app as appmod
import dexadapter


class TestSpotQuotes:
    def test_parses_price_and_mcap_best_pool(self, monkeypatch):
        def fake_get(url, timeout):
            return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"pairs": [
                {"chainId": "solana", "baseToken": {"address": "M1"}, "priceUsd": "0.001",
                 "liquidity": {"usd": 100}, "marketCap": 50000},
                {"chainId": "solana", "baseToken": {"address": "M1"}, "priceUsd": "0.002",
                 "liquidity": {"usd": 900}, "fdv": 90000},          # пул ликвиднее — берём его
                {"chainId": "bsc", "baseToken": {"address": "M2"}, "priceUsd": "5", "liquidity": {"usd": 1}},
            ]})
        monkeypatch.setattr(dexadapter.httpx, "get", fake_get)
        q = dexadapter.spot_quotes(["M1", "M2"])
        assert q["M1"]["price"] == 0.002 and q["M1"]["mcap"] == 90000
        assert "M2" not in q                                        # чужая сеть отфильтрована


class TestLivePricesApi:
    def test_refresh_merges_dex_and_curve(self, monkeypatch):
        appmod._set_live_watch(["D1", "P1pump"])
        monkeypatch.setattr(appmod.dexadapter, "spot_quotes",
                            lambda m: {"D1": dict(price=0.01, mcap=1000)})
        monkeypatch.setattr(appmod.pumpcurve, "usd_prices", lambda m: {"P1pump": 0.00002})
        appmod._LIVE_QUOTES.clear()
        assert appmod._live_refresh_once() is True
        assert appmod._LIVE_QUOTES["D1"]["mcap"] == 1000
        assert abs(appmod._LIVE_QUOTES["P1pump"]["mcap"] - 20000) < 1e-6   # цена × 1B supply

    def test_empty_watch_is_idle(self):
        appmod._set_live_watch([])
        assert appmod._live_refresh_once() is False                 # нет вотчлиста → цикл спит

    def test_endpoint_serves_quotes(self, monkeypatch):
        from starlette.testclient import TestClient
        client = TestClient(appmod.app)
        monkeypatch.setattr(appmod, "ensure_live_prices", lambda: None)   # без фонового потока
        appmod._LIVE_QUOTES.clear()
        appmod._LIVE_QUOTES["CA"] = dict(price=0.1, mcap=123456, ts=1.0)
        d = client.get("/api/prices").json()
        assert d["quotes"]["CA"]["mcap"] == 123456
