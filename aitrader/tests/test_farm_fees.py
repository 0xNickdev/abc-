"""Тесты анти-ферма связки: fee-фильтр (Total Fees vs mcap) + починенный парсинг
GMGN activity (кластеризация откупных кошей). Данные-образцы = живой раг EZ94…pump
(-97%, 3.07 SOL fees, 11/15 кошельков с общей историей)."""
import types

import app as appmod


def _feat(**over):
    base = dict(address="A" * 44, symbol_raw="X", symbol_safe="X", price=0.001,
                mcap=272_000, vol_1h=90_000, age_min=30, chg_1h=0.2, chg_5m=0.05,
                liquidity=50_000, buys=600, sells=400, swaps=1000, buy_ratio=0.6,
                renounced_mint=True, renounced_freeze=True, smart_degen=2, renowned=1,
                sm_confluence=3, gas_fee=-1.0)
    base.update(over)
    return appmod.TokenFeatures(**base)


class TestFeesGate:
    FLT = dict(appmod.DEFAULT_FILTERS, min_fees_per_100k=5.0)

    def test_farm_low_fees_rejected(self):
        # CASHDOG-профиль: 10 SOL fees @ $272k = 3.7/100k < 5 → скам-ферма
        ok, reason, gate = appmod.hard_gates(_feat(gas_fee=10.0), self.FLT, chain="sol")
        assert not ok and "farm wash-trading" in reason

    def test_healthy_fees_pass_gate(self):
        # здоровый: 51 SOL @ $60k = 85/100k — fee-гейт молчит
        ok, reason, _ = appmod.hard_gates(_feat(gas_fee=51.0, mcap=60_000), self.FLT, chain="sol")
        assert "farm wash-trading" not in (reason or "")

    def test_no_data_is_silent(self):
        # gas_fee=-1 (Dex/Mock-адаптер без поля) → фильтр не режет вслепую
        ok, reason, _ = appmod.hard_gates(_feat(gas_fee=-1.0), self.FLT, chain="sol")
        assert "farm wash-trading" not in (reason or "")

    def test_young_microcap_exempt(self):
        # mcap ниже FEES_GATE_MCAP: комиссии ещё не набрались — не режем
        ok, reason, _ = appmod.hard_gates(_feat(gas_fee=0.1, mcap=10_000), self.FLT, chain="sol")
        assert "farm wash-trading" not in (reason or "")

    def test_off_by_default(self):
        ok, reason, _ = appmod.hard_gates(_feat(gas_fee=0.1), appmod.DEFAULT_FILTERS, chain="sol")
        assert "farm wash-trading" not in (reason or "")


class TestFarmPlumbing:
    def test_get_activity_parses_nested_token_address(self):
        # живой gmgn-cli 1.3.9: адрес токена вложен в token.address (раньше парсили token_address → пусто)
        g = types.SimpleNamespace(
            token_traders=lambda addr, order_by="", limit=0: [],
            wallet_activity=lambda w: [
                {"token": {"address": "TOK1"}, "event_type": "buy"},
                {"token": {"address": "TOK2"}, "event_type": "sell"},
                {"token_address": "TOK3"},                 # старый плоский формат тоже понимаем
                {"note": "мусор без токена"},
            ])
        _, ga = appmod._farm_fns(g)
        assert ga("W1") == ["TOK1", "TOK2", "TOK3"]

    def test_wallet_activity_reads_activities_root(self, monkeypatch):
        g = appmod.LiveGMGN.__new__(appmod.LiveGMGN)
        monkeypatch.setattr(g, "_cli", lambda *a: {"activities": [{"token": {"address": "T"}}], "next": ""},
                            raising=False)
        assert g.wallet_activity("W") == [{"token": {"address": "T"}}]

    def test_preentry_farm_flag_blocks_and_caches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(appmod, "BOT_FARM_VERIFY", True)
        monkeypatch.setattr(appmod.MarketLayer, "is_live_adapter", True)
        g = types.SimpleNamespace(token_traders=lambda *a, **k: [])
        monkeypatch.setattr(appmod.MK, "adapter_for", lambda ch: g)
        monkeypatch.setattr(appmod.farm, "detect",
                            lambda addr, gt, ga, max_wallets: calls.append(addr) or
                            dict(red_flag=True, largest_cluster=7, checked=15))
        appmod._FARM_PRE_CACHE.clear()
        flag = appmod._farm_preentry_flag("FARMCA")
        assert flag == "farm cluster 7/15 same-history wallets"
        assert appmod._farm_preentry_flag("FARMCA") == flag   # из кэша
        assert calls == ["FARMCA"]                            # GMGN дёрнут один раз
