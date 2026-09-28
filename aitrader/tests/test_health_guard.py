"""Инцидент 15–28.09: GMGN-бан + вечный дисковый кэш трендинга → бот 13 дней молча стоял.
Покрываем: общий ban-гейт GMGN, срок годности кэша, гейт бота по возрасту данных,
списание зомби-позиций, здоровье/алерты, контрфакт детекторов."""
import subprocess
import time
import types

import pytest
from fastapi import HTTPException
from test_app import _mu_client

import app as appmod
import gmgnguard
import notify
import review

BAN_ERR = ("[gmgn-cli] GET /v1/token/security failed: HTTP 429 code=429 error=RATE_LIMIT_BANNED "
           "message=IP is temporarily banned. Rate limit resets at 2026-09-28 14:09:53 GMT+00:00 "
           "(~300s remaining).")


class TestGuard:
    def test_ban_seconds_from_remaining(self):
        assert gmgnguard.ban_seconds(BAN_ERR) == 310.0

    def test_ban_seconds_from_reset_time_and_default(self):
        import datetime
        now = datetime.datetime(2026, 9, 28, 14, 8, 53, tzinfo=datetime.timezone.utc).timestamp()
        txt = "banned. Rate limit resets at 2026-09-28 14:09:53 GMT+00:00"
        assert gmgnguard.ban_seconds(txt, now_wall=now) == 70.0
        assert gmgnguard.ban_seconds("429 whatever") == gmgnguard.BAN_DEFAULT_S + gmgnguard.BAN_PAD_S

    def test_ban_blocks_all_calls_then_recovers(self):
        g = gmgnguard.Guard(min_interval_s=0.0)
        g.before()
        g.fail(BAN_ERR)
        assert g.banned() and g.snapshot()["down_for_s"] is not None
        with pytest.raises(gmgnguard.GMGNUnavailable) as ei:
            g.before()
        assert appmod._is_rate_limited(ei.value)          # старые фолбэки видят это как 429
        g._banned_until = 0.0
        g.before()
        g.ok()
        snap = g.snapshot()
        assert not snap["banned"] and snap["down_for_s"] is None and snap["last_ok_ago_s"] == 0

    def test_non_ban_error_does_not_pause(self):
        g = gmgnguard.Guard(min_interval_s=0.0)
        g.fail("timeout token info")
        assert not g.banned()

    def test_queue_overflow_fails_fast(self):
        g = gmgnguard.Guard(min_interval_s=10.0, max_wait_s=1.0)
        g.before()                                         # первый слот сразу
        with pytest.raises(gmgnguard.GMGNUnavailable):
            g.before()                                     # следующий через 10с > 1с ожидания


class TestAdaptiveRate:
    def test_ban_slows_down_success_speeds_up(self):
        g = gmgnguard.Guard(min_interval_s=0.35)
        g.fail(BAN_ERR)
        assert g.interval_s == 0.7
        g.fail(BAN_ERR)
        assert g.interval_s == 1.4
        for _ in range(gmgnguard.RECOVER_AFTER_OK):
            g.ok()
        assert abs(g.interval_s - 1.4 / 1.5) < 1e-9
        for _ in range(gmgnguard.RECOVER_AFTER_OK * 10):
            g.ok()
        assert g.interval_s == 0.35                        # не быстрее базового

    def test_interval_capped(self):
        g = gmgnguard.Guard(min_interval_s=0.35)
        for _ in range(20):
            g.fail(BAN_ERR)
        assert g.interval_s == gmgnguard.MAX_INTERVAL_S


class TestLiveGmgnUsesGuard:
    def test_ban_stops_further_subprocess_calls(self, monkeypatch):
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard(min_interval_s=0.0))
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="", stderr=BAN_ERR)
        monkeypatch.setattr(appmod.subprocess, "run", fake_run)
        g = appmod.LiveGMGN("sol")
        with pytest.raises(RuntimeError):
            g.token_security("CA1")
        with pytest.raises(RuntimeError) as ei:
            g.token_security("CA2")
        assert len(calls) == 1                             # второй запрос в сеть не ушёл
        assert "429" in str(ei.value)

    def test_security_is_cached(self, monkeypatch):
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard(min_interval_s=0.0))
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='{"is_honeypot": false}', stderr="")
        monkeypatch.setattr(appmod.subprocess, "run", fake_run)
        g = appmod.LiveGMGN("sol")
        g.token_security("CA1")
        g.token_security("CA1")
        assert len(calls) == 1

    def test_timeout_is_recorded(self, monkeypatch):
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard(min_interval_s=0.0))

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        monkeypatch.setattr(appmod.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            appmod.LiveGMGN("sol").token_info("CA1")
        assert gmgnguard.GUARD.fails == 1 and not gmgnguard.GUARD.banned()


class _BannedAdapter:
    def market_trending(self, cmd=None, **kw):
        raise RuntimeError("gmgn-cli market trending rc=1: 429 RATE_LIMIT_BANNED")


class TestTrendingStaleness:
    def _mk(self, monkeypatch, rows, age_s):
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard(min_interval_s=0.0))
        monkeypatch.setattr(appmod, "_load_trending_disk", lambda ch: (rows, time.time() - age_s))
        mk = appmod.MarketLayer()
        mk.adapter_for = lambda ch: _BannedAdapter()
        return mk

    def test_old_disk_cache_is_not_served_during_ban(self, monkeypatch):
        mk = self._mk(monkeypatch, [{"address": "OLD"}], age_s=13 * 86400)
        assert mk.trending_rows("sol") == []               # 13-дневный снапшот больше не «рынок»

    def test_fresh_disk_cache_still_served_during_ban(self, monkeypatch):
        mk = self._mk(monkeypatch, [{"address": "NEW"}], age_s=30)
        assert mk.trending_rows("sol") == [{"address": "NEW"}]   # деплой ≠ пустой экран
        assert 25 < mk.trending_age("sol") < 60

    def test_disk_roundtrip_keeps_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appmod, "OUT_DIR", tmp_path)
        appmod._save_trending_disk("sol", [{"address": "A"}])
        rows, ts = appmod._load_trending_disk("sol")
        assert rows == [{"address": "A"}] and abs(ts - time.time()) < 5


class TestBotStaleDataGate:
    def test_bot_does_not_buy_on_stale_data(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        monkeypatch.setattr(appmod.MK, "trending_age", lambda ch: 200.0)
        bought = []
        monkeypatch.setattr(appmod, "do_buy", lambda *a, **k: bought.append(a))
        with pytest.raises(HTTPException) as ei:
            appmod._bot_buy_fn(appmod.ST)("sol", "CA_STALE", 0.1)
        assert ei.value.status_code == 409 and "stale market data" in ei.value.detail
        assert not bought


class TestZombiePositions:
    def _setup(self, tmp_path, monkeypatch, opened_ago_h, mode="SHADOW", bot_on=True):
        _mu_client(tmp_path, monkeypatch)

        class _Boom:
            def token_security(self, a): raise RuntimeError("429 RATE_LIMIT_BANNED")
            def token_price(self, a): raise RuntimeError("429")

        class _MKStub:
            is_live_adapter = True
            def adapter_for(self, ch): return _Boom()
        monkeypatch.setattr(appmod, "MK", _MKStub())
        monkeypatch.setattr(appmod.dexadapter, "spot_price", lambda a: 0.0)
        monkeypatch.setattr(appmod.pumpcurve, "usd_prices", lambda m: {})
        s = appmod.get_session("ZombieWallet1111")
        s.mode = mode
        s.bot.enabled = bot_on
        s.positions = [dict(symbol="Z", address="ZOMBIEpump", size_sol=0.1, pnl=0.19, cycles=0,
                            entry=dict(honeypot=False), chain="sol", entry_price=1.0,
                            opened_ts=time.time() - opened_ago_h * 3600)]
        return s

    def test_dead_paper_position_written_off(self, tmp_path, monkeypatch):
        s = self._setup(tmp_path, monkeypatch, opened_ago_h=24)
        out = appmod.monitor_positions("sol", {}, s)
        s.bot.enabled = False
        assert s.positions == [] and out == []
        rec = [ln for ln in appmod.LOG_PATH.read_text().splitlines() if '"SELL"' in ln]
        assert rec and '"pnl": -1.0' in rec[0] and "DEAD" in rec[0]
        assert s.risk.consec_losses == 0                   # пачка зомби не дёргает kill-switch

    def test_recent_position_kept(self, tmp_path, monkeypatch):
        s = self._setup(tmp_path, monkeypatch, opened_ago_h=1)
        appmod.monitor_positions("sol", {}, s)
        s.bot.enabled = False
        assert len(s.positions) == 1

    def test_live_or_manual_never_written_off(self, tmp_path, monkeypatch):
        s = self._setup(tmp_path, monkeypatch, opened_ago_h=24, mode="LIVE")
        appmod.monitor_positions("sol", {}, s)
        assert len(s.positions) == 1
        s2 = self._setup(tmp_path, monkeypatch, opened_ago_h=24, bot_on=False)
        appmod.monitor_positions("sol", {}, s2)
        assert len(s2.positions) == 1
        s.bot.enabled = False


class TestHealth:
    def test_stale_market_raises_alert(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        import time as _t
        bot = types.SimpleNamespace(enabled=True, cfg={"poll_s": 20},
                                    stats={"last_tick": _t.strftime("%Y-%m-%d %H:%M:%S")})
        risk = types.SimpleNamespace(halted=False, consec_losses=0)
        monkeypatch.setattr(appmod, "ST", types.SimpleNamespace(bot=bot, risk=risk, positions=[]))
        mk = types.SimpleNamespace(is_live_adapter=True, trending_age=lambda ch: 3600.0)
        monkeypatch.setattr(appmod, "MK", mk)
        h = appmod.health_snapshot()
        assert any("Market data is 60 min old" in a for a in h["alerts"])

    def test_healthy_has_no_alerts(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard())
        h = appmod.health_snapshot()
        assert h["alerts"] == []

    def test_status_exposes_health(self, tmp_path, monkeypatch):
        c = _mu_client(tmp_path, monkeypatch)
        assert "alerts" in c.get("/api/status").json()["health"]


class TestNotify:
    def test_noop_without_env(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert notify.send("x") is False

    def test_dedup_by_key(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        sent = []
        monkeypatch.setattr(notify, "_post", lambda text: sent.append(text))
        notify.reset("k")
        assert notify.send("a", key="k", cooldown_s=3600) is True
        assert notify.send("b", key="k", cooldown_s=3600) is False
        notify.reset("k")
        assert notify.send("c", key="k", cooldown_s=3600) is True


class TestDetectorEdge:
    def test_counterfactual_saving(self):
        def sell(pnl, checks):
            return dict(action="SELL", pnl=pnl, size_sol=0.1, fraction=1.0,
                        attrib=dict(checks=checks), reason="closed")
        recs = [sell(-0.9, dict(holder=dict(bundled=2))), sell(-0.8, dict(holder=dict(bundled=1))),
                sell(0.5, dict(holder=dict(bundled=0))), sell(-0.2, dict(holder=dict(bundled=0)))]
        d = review.detector_edge(recs)
        r = d["rules"]["holder_bundled"]
        assert d["samples"] == 4 and r["block_rate"] == 0.5
        assert r["saved_sol"] == pytest.approx(0.17)       # блок сберёг бы 0.09+0.08 SOL
        assert review.detector_edge([]) == {}


class TestPreentryChecksRecorded:
    def test_checks_travel_to_buy_attrib(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        monkeypatch.setattr(appmod.dexadapter, "_top10_concentration", lambda a: 0.31)
        monkeypatch.setattr(appmod.dexadapter, "fresh_wallet_count", lambda a: {})
        monkeypatch.setattr(appmod.rugcheck, "check",
                            lambda a: dict(ok=True, insiders=1, bundled=1, danger=[], rugged=False))
        monkeypatch.setattr(appmod, "BOT_FARM_VERIFY", False)
        monkeypatch.setattr(appmod, "BOT_HOLDER_RISK", "log")
        assert appmod.preentry_red_flags("CAchk") is None  # log-режим: не блокирует
        chk = appmod._PREENTRY_CHECKS["CAchk"]
        assert chk["top10"] == 0.31 and chk["holder"]["bundled"] == 1
        assert chk["holder_flag"] == "holder-risk: bundled 1"
        monkeypatch.setattr(appmod, "BOT_HOLDER_RISK", "block")
        assert appmod.preentry_red_flags("CAchk") == "holder-risk: bundled 1"


class TestHealthMore:
    def _st(self, monkeypatch, **kw):
        import time as _t
        bot = types.SimpleNamespace(enabled=kw.get("enabled", True), cfg={"poll_s": 20},
                                    stats=dict(last_tick=kw.get("last_tick", _t.strftime("%Y-%m-%d %H:%M:%S")),
                                               last_error=kw.get("last_error")))
        risk = types.SimpleNamespace(halted=kw.get("halted", False), consec_losses=0)
        monkeypatch.setattr(appmod, "ST", types.SimpleNamespace(bot=bot, risk=risk,
                                                                positions=kw.get("positions", [])))
        monkeypatch.setattr(appmod, "MK", types.SimpleNamespace(is_live_adapter=True,
                                                                trending_age=lambda ch: 5.0))
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard())

    def test_autostart_bot_off(self, monkeypatch):
        self._st(monkeypatch, enabled=False)
        monkeypatch.setenv("BOT_AUTOSTART", "n2")
        assert any(a.startswith("House bot is OFF") for a in appmod.health_snapshot()["alerts"])

    def test_stuck_loop_and_kill_switch(self, monkeypatch):
        self._st(monkeypatch, last_tick="2026-01-01 00:00:00", last_error="boom", halted=True)
        al = appmod.health_snapshot()["alerts"]
        assert any(a.startswith("Bot loop stuck") and "boom" in a for a in al)
        assert any(a.startswith("Kill-switch") for a in al)

    def test_frozen_position_price(self, monkeypatch):
        self._st(monkeypatch, positions=[dict(symbol="FRZ", px_ts=time.time() - 3600)])
        assert any("FRZ" in a for a in appmod.health_snapshot()["alerts"])


class TestPersistence:
    def test_risk_state_survives_restart(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        appmod.ST.risk.consec_losses = 4
        appmod._RECENT_EXITS[("local", "RUGCA")] = (time.time(), -0.6)
        monkeypatch.setattr(appmod.WEEK_BUDGET, "loss", 3.3)
        appmod.save_risk_state()
        appmod.ST.risk.consec_losses = 0
        appmod._RECENT_EXITS.clear()
        monkeypatch.setattr(appmod.WEEK_BUDGET, "loss", 0.0)
        appmod.load_risk_state()
        assert appmod.ST.risk.consec_losses == 4 and appmod.WEEK_BUDGET.loss == 3.3
        assert "24h" in appmod._reentry_block("local", "RUGCA")

    def test_corrupt_positions_quarantined_not_overwritten(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        appmod.POSITIONS_PATH.write_text('[{"symbol": "X", "addr')      # обрезанный JSON
        assert appmod.load_positions() == []
        assert not appmod.POSITIONS_PATH.exists()
        assert list(tmp_path.glob("positions.json.corrupt-*"))

    def test_session_key_never_overwritten(self, tmp_path):
        import sessionwallet
        kp = tmp_path / "session_key.json"
        kp.write_text("garbage")
        with pytest.raises(RuntimeError):
            sessionwallet._load_or_create(kp)
        assert kp.read_text() == "garbage"


class TestGuardThrottleIsNotBan:
    def test_trending_serves_cache_without_cooldown_on_local_throttle(self, monkeypatch):
        monkeypatch.setattr(gmgnguard, "GUARD", gmgnguard.Guard(min_interval_s=0.0))
        monkeypatch.setattr(appmod, "_load_trending_disk", lambda ch: ([{"address": "A"}], time.time() - 30))
        mk = appmod.MarketLayer()

        class _T:
            def market_trending(self, cmd=None, **kw):
                raise gmgnguard.GMGNThrottled("429 GMGN local throttle: queue 20s")
        mk.adapter_for = lambda ch: _T()
        assert mk.trending_rows("sol") == [{"address": "A"}]
        assert "sol" not in mk._trending_cooldown


class TestN3ShadowIsPaper:
    def test_n3_in_shadow_never_signs(self, tmp_path, monkeypatch):
        _mu_client(tmp_path, monkeypatch)
        sess = appmod.get_session("N3ShadowWallet")
        sess.bot.cfg["mode"] = "n3"
        monkeypatch.setattr(appmod, "LIVE_TRADING_DISABLED", False)
        signed = []
        monkeypatch.setattr(appmod.sessionwallet, "sign_and_send", lambda tx, kp: signed.append(1))
        monkeypatch.setattr(appmod, "do_buy", lambda *a, **k: dict(ok=True, paper=True))
        res = appmod._n3_execute(sess, "buy", "sol", "CA", size_sol=0.05)
        assert res.get("paper") and not signed
