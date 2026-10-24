"""24.10: стопы −47/−66% на рагах. Цена позиций — из своего потока (реакция на каждую сделку),
выход после крупной продажи одной транзакцией и из «мёртвого» токена."""
import threading
import time
import types

import bot
import pumpflow
import watcher as w

MINT = "Shk1111111111111111111111111111111111111pump"


class TestFlowSignals:
    def flow(self):
        f = pumpflow.PumpFlow()
        mf = f._get(MINT, time.time())
        mf.venue = "amm"
        return f, mf

    def test_big_sell_recorded_and_watch_kicked(self):
        f, mf = self.flow()
        kicks = []
        f.watch, f.on_watch = {MINT}, kicks.append
        now = time.time()
        mf.price_sol = 1e-6
        old = mf.price_sol
        mf.price_sol = 0.53e-6                                   # одна продажа −47%
        f._after_trade(mf, old, False, int(10e9), int(30e9), now)
        assert kicks == [MINT]
        ts, drop, share = f.shock_since(MINT, now - 1)
        assert drop == 0.47 and share == 0.25
        assert f.shock_since(MINT, now + 1) is None              # до входа — не наш слив

    def test_small_sells_and_buys_not_recorded(self):
        f, mf = self.flow()
        mf.price_sol = 1e-6
        f._after_trade(mf, 1.03e-6, False, int(1e9), int(100e9), time.time())   # −3%, 1% пула
        f._after_trade(mf, 0.5e-6, True, int(50e9), int(100e9), time.time())    # покупка
        assert f.shock_since(MINT, 0) is None

    def test_dead_stats_needs_full_window(self):
        f, mf = self.flow()
        mf.price_sol = 1e-6                                      # капа 1000 SOL
        f._started_at = time.time() - 60
        assert f.dead_stats(MINT) is None                        # поток только запущен
        f._started_at = time.time() - 4000
        now = time.time()
        mf.buckets.append([now - 100, 1, 1, int(1e9), int(1e9)])                # 2 SOL за 30 мин
        mf.candles.extend([[now - 600, 1e-6, 1.02e-6, 0.99e-6, 1e-6, 0], [now - 60, 1e-6, 1.01e-6, 1e-6, 1e-6, 0]])
        d = f.dead_stats(MINT)
        assert d["turnover"] == 0.002 and abs(d["range"] - 0.0303) < 1e-3


class TestMidFlowExits:
    def pos(self, **kw):
        return dict(dict(market="mid", pnl=-0.05, peak_pnl=0.02, opened_ts=time.time() - 3600, tp_taken=[]), **kw)

    rc = dict(hard_stop_pct=0.5, trailing_pct=0.25, tp_ladder=[])

    def test_shock_exit(self):
        assert "shock" in bot.decide_exit(self.pos(flow_shock=[0, 0.19, 0.05]), None, self.rc).reason
        assert "shock" in bot.decide_exit(self.pos(flow_shock=[0, 0.05, 0.30]), None, self.rc).reason
        assert bot.decide_exit(self.pos(flow_shock=[0, 0.10, 0.10]), None, self.rc).action == "HOLD"

    def test_shock_exit_also_for_runner(self):
        ed = bot.decide_exit(self.pos(pnl=0.9, peak_pnl=1.3, tp_taken=[0], flow_shock=[0, 0.2, 0.1]), None, self.rc)
        assert ed.action == "SELL" and ed.fraction == 1.0

    def test_dead_exit(self):
        dead = dict(turnover=0.001, range=0.03)
        assert "dead-token" in bot.decide_exit(self.pos(flow_dead=dead), None, self.rc).reason
        young = self.pos(flow_dead=dead, opened_ts=time.time() - 600)          # держим меньше 30 мин
        assert bot.decide_exit(young, None, self.rc).action == "HOLD"
        moving = self.pos(flow_dead=dict(turnover=0.001, range=0.07))         # цена ходит — не мёртвый
        assert bot.decide_exit(moving, None, self.rc).action == "HOLD"
        assert bot.decide_exit(self.pos(flow_dead=dict(turnover=0.02, range=0.01)), None, self.rc).action == "HOLD"


class _Sess:
    def __init__(self, positions):
        self.positions = positions
        self.lock = threading.Lock()
        self.bot = types.SimpleNamespace(enabled=True, cfg=dict(bot.CFG))


class TestWatcherFlowPrice:
    def setup(self, monkeypatch, p, flow_px, dex_px):
        monkeypatch.setattr(w.geyser.FEED, "set_mints", lambda m: None)
        monkeypatch.setattr(w.geyser.FEED, "prices_usd", lambda m: {})
        monkeypatch.setattr(w.pumpcurve, "usd_prices", lambda m: {})
        self.dex_calls = []
        monkeypatch.setattr(w.dexadapter, "spot_prices", lambda m: self.dex_calls.append(m) or ({MINT: dex_px} if dex_px else {}))
        f = pumpflow.PumpFlow()
        mf = f._get(MINT, time.time())
        mf.price_sol = flow_px
        monkeypatch.setattr(pumpflow, "FLOW", f)
        self.sold = []
        s = _Sess([p])
        pw = w.PositionWatcher(lambda: [s], dict(hard_stop_pct=0.5), lambda sess: (lambda a, fraction, reason: self.sold.append(reason)))
        return pw, f, mf

    def test_new_position_anchors_on_flow_and_stops_fast(self, monkeypatch):
        p = dict(address=MINT, entry_price=0.002, chain="sol", market="mid", auto=True, opened_ts=time.time() - 5)
        pw, f, mf = self.setup(monkeypatch, p, 1e-5, 0.0021)
        pw.poll_once()
        assert p["entry_flow_sol"] == 1e-5 and p["pnl"] == 0.0 and p["px_src"] == "flow"
        assert f.watch == {MINT}
        mf.price_sol = 0.79e-5                                   # поток: −21% (DexScreener ещё старый)
        pw.poll_once()
        assert p["pnl"] == -0.21 and self.sold and "Mid stop" in self.sold[0]
        assert len(self.dex_calls) == 1                          # после якоря DexScreener не нужен

    def test_old_position_keeps_dex_pnl_when_anchoring(self, monkeypatch):
        p = dict(address=MINT, entry_price=0.002, chain="sol", market="mid", auto=True, opened_ts=time.time() - 3600)
        pw, f, mf = self.setup(monkeypatch, p, 1e-5, 0.0022)     # по Dex +10%
        pw.poll_once()
        assert abs(p["pnl"] - 0.10) < 1e-9 and abs(p["entry_flow_sol"] - 1e-5 / 1.1) < 1e-15
