"""Аудит 28.09: скан под замком сессии блокировал real-time стопы на 20-40с.
Проверяем, что сеть идёт без замка, а TP-ступени не теряются/не дублируются."""
import threading
import time

import bot as botmod
import watcher as w


def _runner(screen, buy, sell, positions, halted=lambda: False):
    r = botmod.BotRunner()
    lock = threading.Lock()
    r._fns = dict(screen=lambda ch: screen(lock), buy=lambda ch, a, sz: buy(lock, a),
                  sell=lambda a, fraction=1.0, reason=None: sell(lock, a, fraction),
                  positions=lambda: positions, risk_cfg=dict(hard_stop_pct=0.25, trailing_pct=0.25,
                                                             tp_ladder=[[0.6, 0.4]], max_concurrent_positions=5),
                  lock=lock, halted=halted)
    r.cfg.update(require_abc_trigger=False, min_priority=0, max_new_per_tick=2)
    return r, lock


def _decision(addr):
    return dict(decision=dict(address=addr, action="ACTION", size_sol=0.1, symbol=addr,
                              priority=90, abc=dict(trigger=True)))


class TestTickLocking:
    def test_screen_and_buy_run_without_lock_exits_with_lock(self):
        seen = {}
        positions = [dict(address="P1", pnl=-0.5, chain="sol")]

        def screen(lock):
            seen["screen_locked"] = lock.locked()
            return dict(decisions=[_decision("NEW1")], positions=[])

        def buy(lock, a):
            seen["buy_locked"] = lock.locked()
            return dict(ok=True)

        def sell(lock, a, fraction):
            seen["sell_locked"] = lock.locked()
            positions.clear()
            return dict(ok=True, closed=True)

        r, _ = _runner(screen, buy, sell, positions)
        r.tick()
        assert seen == dict(screen_locked=False, buy_locked=False, sell_locked=True)
        assert r.stats["buys"] == 1 and r.stats["sells"] == 1


class TestTpRungBookkeeping:
    def _pos(self):
        return dict(address="T1", pnl=0.7, peak_pnl=0.7, chain="sol", tp_taken=[])

    def test_proposed_sell_does_not_consume_rung(self):
        p = self._pos()
        r, _ = _runner(lambda lk: dict(decisions=[], positions=[]), None,
                       lambda lk, a, f: dict(ok=True, proposed=True), [p], halted=lambda: True)
        r.tick()
        assert p["tp_taken"] == []

    def test_failed_sell_rolls_back_rung(self):
        p = self._pos()

        def boom(lk, a, f):
            raise RuntimeError("rpc down")
        r, _ = _runner(lambda lk: dict(decisions=[], positions=[]), None, boom, [p], halted=lambda: True)
        r.tick()
        assert p["tp_taken"] == [] and r.stats["errors"] == 1

    def test_rung_recorded_before_sell_persists(self):
        p = self._pos()
        seen = {}

        def sell(lk, a, f):
            seen["taken_at_sell"] = list(p["tp_taken"])      # do_sell пишет диск в этот момент
            return dict(ok=True, closed=False)
        r, _ = _runner(lambda lk: dict(decisions=[], positions=[]), None, sell, [p], halted=lambda: True)
        r.tick()
        assert seen["taken_at_sell"] == [0] and p["tp_taken"] == [0]


class _Sess:
    def __init__(self, positions):
        self.positions = positions
        self.lock = threading.Lock()
        self.bot = type("B", (), {"enabled": True, "cfg": {"escape_severity_exit": 60,
                                                           "trail_activate_pct": 0.3}})()


class TestWatcherStalePrice:
    def test_price_not_applied_after_long_lock_wait(self, monkeypatch):
        p = dict(address="MINT1", entry_price=1.0, cur_price=1.0, pnl=0.0, chain="sol")
        s = _Sess([p])
        pw = w.PositionWatcher(lambda: [s], dict(hard_stop_pct=0.25), lambda sess: None)
        monkeypatch.setattr(w.dexadapter, "spot_prices", lambda m: {"MINT1": 0.9})
        monkeypatch.setattr(w, "STALE_AFTER_WAIT_S", 0.05)
        s.lock.acquire()
        threading.Timer(0.2, s.lock.release).start()        # скан «держит» замок 0.2с
        pw.poll_once()
        assert p["pnl"] == 0.0 and pw.stats["stale_skips"] == 1
        time.sleep(0.01)
        pw.poll_once()                                      # замок свободен → цена применяется
        assert p["pnl"] == -0.1
