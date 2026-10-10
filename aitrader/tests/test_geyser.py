"""Geyser (Shyft Yellowstone gRPC): разбор кривой, подписка, свежесть, приоритет в watcher,
перебазирование цены входа; SHADOW без потолка позиций."""
import struct
import threading
import time
import types

import pytest

import app as appmod
import geyser
import watcher as w

MINT = "BzLtiUrd4ex7B8YydzxnbUmr6je1AKWhsKwtJR7ppump"


def _curve_bytes(vtok: int, vsol: int, complete: bool = False) -> bytes:
    raw = bytearray(8) + struct.pack("<Q", vtok) + struct.pack("<Q", vsol) + bytearray(24)
    raw += bytes([1 if complete else 0])
    return bytes(raw)


class TestParse:
    def test_price_from_reserves(self):
        px, done = geyser.parse_curve_bytes(_curve_bytes(1_073_000_000 * 10**6, 30 * 10**9))
        assert px == pytest.approx(30 / 1_073_000_000) and done is False

    def test_complete_and_garbage(self):
        assert geyser.parse_curve_bytes(_curve_bytes(10**12, 10**9, complete=True))[1] is True
        assert geyser.parse_curve_bytes(b"\x00") == (0.0, False)


class TestFeed:
    def _feed(self, monkeypatch):
        monkeypatch.setattr(geyser.pumpcurve, "sol_usd", lambda: 100.0)
        f = geyser.GeyserFeed()
        f.set_mints([MINT, "NotPumpMint111", ""])
        return f

    def _update(self, f, raw):
        pda = next(iter(f._want))
        import base58
        upd = types.SimpleNamespace(
            HasField=lambda name: name == "account",
            account=types.SimpleNamespace(account=types.SimpleNamespace(
                pubkey=base58.b58decode(pda), data=raw)))
        f._handle(upd)

    def test_only_pump_mints_subscribed(self, monkeypatch):
        f = self._feed(monkeypatch)
        assert list(f._want.values()) == [MINT]

    def test_update_gives_usd_price_and_kicks(self, monkeypatch):
        f = self._feed(monkeypatch)
        kicked = []
        f._on_update = kicked.append
        self._update(f, _curve_bytes(10**15, 3 * 10**10))      # 30 SOL / 1e9 токенов
        assert f.prices_usd([MINT])[MINT] == pytest.approx(3e-8 * 100)
        assert kicked == [MINT]

    def test_stale_and_completed_not_served(self, monkeypatch):
        f = self._feed(monkeypatch)
        self._update(f, _curve_bytes(10**15, 3 * 10**10))
        f._prices[MINT] = (f._prices[MINT][0], time.time() - 120, False)
        assert f.prices_usd([MINT]) == {}
        self._update(f, _curve_bytes(10**15, 3 * 10**10, complete=True))
        assert f.prices_usd([MINT]) == {}                       # мигрировал → цену даёт AMM

    def test_resubscribe_queued_on_change(self, monkeypatch):
        f = self._feed(monkeypatch)
        while not f._q.empty():
            f._q.get_nowait()
        f.set_mints([MINT])                                     # тот же состав — без запроса
        assert f._q.empty()
        f.set_mints([])
        assert f._q.get_nowait() == "resub" and f._want == {}

    def test_request_contains_curve_accounts(self, monkeypatch):
        f = self._feed(monkeypatch)
        req = f._request()
        assert list(req.accounts["positions"].account) == list(f._want)


class _Sess:
    def __init__(self, positions):
        self.positions = positions
        self.lock = threading.Lock()
        self.bot = types.SimpleNamespace(enabled=False, cfg={})


class TestWatcherUsesGeyser:
    def test_geyser_first_and_entry_rebased(self, monkeypatch):
        p = dict(address=MINT, entry_price=1e-7, cur_price=1e-7, pnl=0.0, chain="sol",
                 opened_ts=time.time() - 5)
        s = _Sess([p])
        monkeypatch.setattr(w.geyser.FEED, "set_mints", lambda m: None)
        monkeypatch.setattr(w.geyser.FEED, "prices_usd", lambda m: {MINT: 4e-6})
        called = []
        monkeypatch.setattr(w.dexadapter, "spot_prices", lambda m: called.append(m) or {})
        pw = w.PositionWatcher(lambda: [s], dict(hard_stop_pct=0.25), lambda sess: None)
        pw.poll_once()
        assert called == []                                     # всё покрыто Geyser → DexScreener не нужен
        assert p["entry_price"] == 4e-6 and p["entry_price_orig"] == 1e-7
        assert p["pnl"] == 0.0 and p["px_src"] == "geyser"     # фальшивых ×40 больше нет

    def test_no_rebase_after_window(self, monkeypatch):
        p = dict(address=MINT, entry_price=1e-6, cur_price=1e-6, pnl=0.0, chain="sol",
                 opened_ts=time.time() - 3600)
        s = _Sess([p])
        monkeypatch.setattr(w.geyser.FEED, "set_mints", lambda m: None)
        monkeypatch.setattr(w.geyser.FEED, "prices_usd", lambda m: {MINT: 2e-6})
        pw = w.PositionWatcher(lambda: [s], dict(hard_stop_pct=0.25), lambda sess: None)
        pw.poll_once()
        assert p["entry_price"] == 1e-6 and p["pnl"] == 1.0


class TestShadowUnlimited:
    def test_shadow_no_position_cap_live_capped(self, tmp_path, monkeypatch):
        from test_app import _mu_client
        _mu_client(tmp_path, monkeypatch)
        s = appmod.ST
        s.mode = "SHADOW"
        monkeypatch.setattr(appmod, "SHADOW_FUSES", False)
        ok, _ = s.risk.gate(0.1, 500, 999.0, fuses=appmod._fuses_on(s))
        assert ok and appmod._max_positions(s) >= 10 ** 6
        s.mode = "LIVE"
        ok, why = s.risk.gate(0.1, 500, 0.0, fuses=appmod._fuses_on(s))
        assert not ok and "concurrent" in why
        assert appmod._max_positions(s) == appmod.CFG["max_concurrent_positions"]
        s.mode = "SHADOW"
