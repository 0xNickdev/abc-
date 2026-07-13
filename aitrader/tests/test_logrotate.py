"""Тесты ротации журнала и потоковой статистики: данные НЕ теряются.

Смысл фикса: журнал растёт 24/7 (FILTER/SCREEN каждый тик), а эндпоинты статистики
раньше парсили его целиком в память (8 ГБ RSS на Railway). Теперь старое переливается
в gzip-архивы, статистика читает hot + архивы потоково — история доступна вся.
"""
import datetime
import gzip
import json

import app as appmod
import backtest


def _iso(days_ago: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


def _sell(ts, pnl, size=0.5, sym="A"):
    return dict(ts=ts, action="SELL", symbol=sym, reason="x", pnl=pnl, size_sol=size,
                pubkey=appmod.DEFAULT_PUBKEY)


def _filter(ts):
    return dict(ts=ts, action="FILTER", symbol="F", reason="REJECT gate")


def _write_log(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestRotation:
    def _setup(self, tmp_path, monkeypatch, rows, rotate_mb=0.001):
        monkeypatch.setattr(appmod, "LOG_PATH", tmp_path / "trade_decisions.jsonl")
        monkeypatch.setattr(appmod, "LOG_ROTATE_MB", rotate_mb)
        _write_log(appmod.LOG_PATH, rows)
        return appmod.LOG_PATH

    def test_old_records_move_to_archive_nothing_lost(self, tmp_path, monkeypatch):
        old, fresh = _iso(30), _iso(0)
        rows = ([_filter(old) for _ in range(10)]
                + [_sell(old, 0.2), _sell(old, -0.1)]
                + [_sell(fresh, 0.3), _sell(fresh, -0.05)])
        p = self._setup(tmp_path, monkeypatch, rows)
        appmod._rotate_log_locked()
        # архив появился, hot ужался
        arch = list((tmp_path / backtest.ARCHIVE_DIRNAME).glob("trade_decisions-*.jsonl.gz"))
        assert arch, "старые записи должны уехать в gzip-архив"
        assert p.stat().st_size < sum(len(json.dumps(r)) + 1 for r in rows)
        # статистика видит ВСЮ историю: hot + архивы
        assert len(backtest.sell_records(p)) == 4
        s = backtest.summary(p)
        assert s["records"] == len(rows)
        assert s["realized"]["trades"] == 4

    def test_rotation_below_threshold_is_noop(self, tmp_path, monkeypatch):
        rows = [_sell(_iso(0), 0.1)]
        p = self._setup(tmp_path, monkeypatch, rows, rotate_mb=10)
        before = p.read_text()
        appmod._rotate_log_locked()
        assert p.read_text() == before
        assert not (tmp_path / backtest.ARCHIVE_DIRNAME).exists()

    def test_double_rotation_appends_readable_gzip(self, tmp_path, monkeypatch):
        old = _iso(30)
        p = self._setup(tmp_path, monkeypatch,
                        [_filter(old) for _ in range(10)] + [_sell(old, 0.2)])
        appmod._rotate_log_locked()
        # вторая волна старых записей → дозапись в тот же месячный архив (gzip-конкатенация)
        with p.open("a") as fh:
            for r in [_filter(old) for _ in range(10)] + [_sell(old, -0.3)]:
                fh.write(json.dumps(r) + "\n")
        appmod._rotate_log_locked()
        assert len(backtest.sell_records(p)) == 2
        assert backtest.summary(p)["records"] == 22

    def test_pnl_calendar_sees_archived_month(self, tmp_path, monkeypatch):
        old = _iso(40)
        month = old[:7]
        self._setup(tmp_path, monkeypatch,
                    [_filter(old) for _ in range(10)] + [_sell(old, 0.2, size=1.0)])
        appmod._rotate_log_locked()
        cal = appmod.pnl_calendar("*", month)
        assert cal["total"]["trades"] == 1
        assert abs(cal["total"]["pnl"] - 0.2) < 1e-6


class TestScanCache:
    def test_cache_invalidates_on_append(self, tmp_path, monkeypatch):
        monkeypatch.setattr(appmod, "LOG_PATH", tmp_path / "trade_decisions.jsonl")
        _write_log(appmod.LOG_PATH, [_sell(_iso(0), 0.1)])
        assert len(backtest.sell_records(appmod.LOG_PATH)) == 1
        with appmod.LOG_PATH.open("a") as fh:
            fh.write(json.dumps(_sell(_iso(0), -0.2)) + "\n")
        assert len(backtest.sell_records(appmod.LOG_PATH)) == 2

    def test_gzip_archive_is_scanned(self, tmp_path):
        hot = tmp_path / "trade_decisions.jsonl"
        _write_log(hot, [_sell(_iso(0), 0.1)])
        ad = tmp_path / backtest.ARCHIVE_DIRNAME
        ad.mkdir()
        with gzip.open(ad / "trade_decisions-2026-01.jsonl.gz", "at", encoding="utf-8") as gz:
            gz.write(json.dumps(_sell("2026-01-05T10:00:00+00:00", 0.5)) + "\n")
        assert len(backtest.sell_records(hot)) == 2
