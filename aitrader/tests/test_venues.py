"""venues + разбор по балансам: GeckoTerminal → список наблюдения; сделки Raydium/Meteora по
изменению балансов подписанта; подписка стрима на токены; сверка в learn.mid_scan."""
import pathlib
import sys
import time

import base58
import pytest

import geyser
import pumpflow as pf
import venues as vn

sys.path.insert(0, str(pathlib.Path(geyser.__file__).resolve().parent / "ysproto"))
import geyser_pb2 as pb  # noqa: E402

TOKEN = "RayTok1111111111111111111111111111111111111"
SIGNER = base58.b58encode(b"\x31" * 32).decode()
RAY = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def pool(mint, quote=vn.SOL_MINT, mcap=500_000, liq=50_000, created="2026-10-15T00:00:00Z", vol=1e6, dex="raydium"):
    return dict(attributes=dict(address="P" + mint[:5], market_cap_usd=mcap, fdv_usd=mcap, reserve_in_usd=liq,
                                volume_usd=dict(h24=vol), base_token_price_usd=0.001, pool_created_at=created),
                relationships=dict(base_token=dict(data=dict(id="solana_" + mint)),
                                   quote_token=dict(data=dict(id="solana_" + quote)),
                                   dex=dict(data=dict(id=dex))))


class TestGecko:
    NOW = 1_792_000_000.0          # 2026-10-15

    def test_parse_filters(self):
        data = dict(data=[pool("Good1"), pool("Usdc1", quote="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
                          pool("Small1", mcap=100_000), pool("Thin1", liq=2_000),
                          pool("Old1", created="2026-09-01T00:00:00Z"), pool("Pump1", dex="pumpswap")])
        assert [c["mint"] for c in vn.parse_pools(data, None, self.NOW)] == ["Good1"]

    def test_refresh_change_expire_and_cap(self, monkeypatch):
        got = []
        pages = {"raydium": [pool("A"), pool("B", vol=5)], "meteora": [pool("C", vol=9, dex="meteora")]}
        w = vn.VenueWatch(fetch=lambda url: dict(data=[x for v in pages.values() for x in v]
                                                 if "new_pools?page=1&" in url else []),
                          on_change=got.append)
        assert w.refresh(now=self.NOW) is True and got[-1] == ["A", "B", "C"]
        assert w.refresh(now=self.NOW + 60) is False                     # без изменений — без пересписки
        pages.clear()
        w.refresh(now=self.NOW + vn.EXPIRE_S + 120)
        assert w.mints() == []
        monkeypatch.setattr(vn, "WATCH_CAP", 1)
        pages.update(raydium=[pool("A", vol=1), pool("B", vol=99)])
        w.refresh(now=self.NOW + 9000)
        assert w.mints() == ["B"]                                       # кап — по объёму

    def test_fetch_error_counted(self):
        w = vn.VenueWatch(fetch=lambda url: (_ for _ in ()).throw(RuntimeError("429")))
        w.refresh(now=self.NOW)
        assert w.stats["errors"] == len(vn.LISTS)


def tx(sol_delta, tok_pre, tok_post, mint=TOKEN, wsol=(0, 0), program=RAY, extra_mint=None):
    u = pb.SubscribeUpdate()
    t = u.transaction
    t.slot = 1
    msg = t.transaction.transaction.message
    msg.account_keys.extend([base58.b58decode(SIGNER), base58.b58decode(program)])
    meta = t.transaction.meta
    meta.fee = 5000
    meta.pre_balances.extend([10 * 10**9, 1])
    meta.post_balances.extend([10 * 10**9 + sol_delta - 5000, 1])
    for lst, amt, wamt in ((meta.pre_token_balances, tok_pre, wsol[0]), (meta.post_token_balances, tok_post, wsol[1])):
        b = lst.add(); b.mint, b.owner = mint, SIGNER; b.ui_token_amount.amount = str(amt); b.ui_token_amount.decimals = 9
        if wsol != (0, 0):
            b = lst.add(); b.mint, b.owner = pf.SOL_MINT, SIGNER; b.ui_token_amount.amount = str(wamt); b.ui_token_amount.decimals = 9
        if extra_mint:
            b = lst.add(); b.mint, b.owner = extra_mint, SIGNER; b.ui_token_amount.amount = str(amt * 2); b.ui_token_amount.decimals = 6
    return u


class TestBalanceTrades:
    W = {TOKEN}

    def test_buy_and_sell(self):
        (b,) = pf.balance_trades(tx(-2 * 10**9, 0, 4 * 10**9).transaction.transaction, self.W)
        assert b["is_buy"] and b["lamports"] == 2 * 10**9 and b["tokens"] == 4 * 10**9 and b["venue"] == "raydium"
        (s,) = pf.balance_trades(tx(10**9, 4 * 10**9, 3 * 10**9).transaction.transaction, self.W)
        assert not s["is_buy"] and s["lamports"] == 10**9 and s["tokens"] == 10**9

    def test_wsol_counts_as_sol(self):
        (b,) = pf.balance_trades(tx(0, 0, 10**9, wsol=(3 * 10**9, 10**9)).transaction.transaction, self.W)
        assert b["is_buy"] and b["lamports"] == 2 * 10**9

    def test_skips_arb_dust_unwatched_and_multi_token(self):
        assert pf.balance_trades(tx(10**9, 5, 5).transaction.transaction, self.W) == []            # токен не изменился
        assert pf.balance_trades(tx(-50, 0, 10).transaction.transaction, self.W) == []              # пыль
        assert pf.balance_trades(tx(-10**9, 0, 10).transaction.transaction, {"Other"}) == []
        assert pf.balance_trades(tx(-10**9, 0, 10, extra_mint="Tok2").transaction.transaction, {TOKEN, "Tok2"}) == []

    def test_ingest_updates_flow(self):
        f = pf.PumpFlow()
        f._tracked_set = lambda: {}
        f._started_at = time.time() - 4000
        f.set_watch([TOKEN, "Xpump"])
        assert f._watch == {TOKEN}                                       # pump-токены идут своим путём
        f.ingest(tx(-2 * 10**9, 0, 4 * 10**9))
        f.ingest(tx(10**9, 4 * 10**9, 3 * 10**9))
        m = f.metrics(TOKEN)
        assert m["venue"] == "raydium" and (m["buys"], m["sells"]) == (1, 1)
        assert m["price_sol"] == pytest.approx(1.0) and m["mcap_sol"] is None


def test_geyser_watch_filter(monkeypatch):
    monkeypatch.delenv("ABC_FLOW", raising=False)
    feed = geyser.GeyserFeed()
    assert "watch" not in feed._request().transactions
    feed.set_watch([TOKEN, "Ypump"])
    req = feed._request()
    assert list(req.transactions["watch"].account_include) == [TOKEN] and feed._q.get_nowait() == "resub"
    feed.set_watch([TOKEN])
    assert feed._q.empty()                                               # тот же список — без пересписки


def test_outlier_trade_does_not_move_price():
    f = pf.PumpFlow()
    f._tracked_set = lambda: {}
    f.set_watch([TOKEN])
    f.ingest(tx(-2 * 10**9, 0, 4 * 10**9))                 # 2 SOL за 4 токена = 0.5 SOL/токен
    f.ingest(tx(-2 * 10**9, 4 * 10**9, 4 * 10**9 + 10**7))  # цена ×200 — кривая сделка
    assert f.metrics(TOKEN)["price_sol"] == pytest.approx(0.5) and f.stats["venue_outliers"] == 1
