"""learn.Recorder: образцы по всем кандидатам (и отклонённым), дедуп, разметка исхода по потоку
(максимум/минимум, первое касание уровней, контрольные точки), запись в jsonl по горизонту."""
import json

import learn


class FakeFlow:
    def __init__(self):
        self.px = {}
        self.hot_rows = []

    def price(self, mint):
        return (self.px[mint], 1.0) if mint in self.px else None

    def entry_snapshot(self, mint):
        return dict(snipers=2, dev_supply_pct=4.0) if mint in self.px else None

    def hot(self, window_s, n):
        return self.hot_rows

    def mid_candidates(self, min_mcap_sol, min_liq_sol, active_s=300):
        self.mid_args = (min_mcap_sol, min_liq_sol)
        return [c for c in getattr(self, "mids", []) if c[1] >= min_mcap_sol and c[2] >= min_liq_sol]


def dec(mint, action="SKIP", gate=2, reason="REJECT safety: top10"):
    return dict(decision=dict(address=mint, action=action, gate=gate, reason=reason, features=dict(liquidity=9000)))


def rec(tmp_path):
    f = FakeFlow()
    r = learn.Recorder(tmp_path, flow=f)
    r.start = lambda: None
    return r, f


def test_records_rejected_and_passed_with_features(tmp_path):
    r, f = rec(tmp_path)
    f.px.update(A=1.0, B=2.0)
    r.observe_decisions([dec("A"), dec("B", "ACTION", None, "Passed all gates"), dec("C")])
    assert len(r.pending) == 2 and r.stats["skipped_no_flow"] == 1          # C — нет в потоке
    s = next(v for v in r.pending.values() if v["mint"] == "A")
    assert s["decision"]["gate"] == 2 and s["gmgn"]["liquidity"] == 9000 and s["flow"]["snipers"] == 2


def test_dedup_per_mint(tmp_path):
    r, f = rec(tmp_path)
    f.px["A"] = 1.0
    r.observe_decisions([dec("A")])
    r.observe_decisions([dec("A")])
    assert len(r.pending) == 1


def test_labels_path_and_writes_at_horizon(tmp_path):
    r, f = rec(tmp_path)
    f.px["A"] = 1.0
    r._add("A", "gmgn", None, None, now=1000.0)
    for t, p in ((1060, 1.6), (1100, 0.6), (1300, 0.4), (1950, 0.9), (4600, 0.5)):
        f.px["A"] = p
        r.label_tick(now=t)
    assert not r.pending and r.stats["labeled"] == 1
    out = [json.loads(x) for x in (tmp_path / "learn").glob("samples-*.jsonl").__next__().read_text().splitlines()]
    s = out[0]
    assert s["hi"] == 0.6 and s["lo"] == -0.6
    assert s["up"] == {"0.1": 60, "0.2": 60, "0.3": 60, "0.5": 60}
    assert s["dn"] == {"-0.1": 100, "-0.15": 100, "-0.2": 100, "-0.35": 100, "-0.5": 300}
    assert s["at"]["120"] == -0.6 and s["at"]["300"] == -0.6 and s["at"]["900"] == -0.1 and s["at"]["3600"] == -0.5


def test_flow_top_adds_tokens_beyond_gmgn(tmp_path):
    r, f = rec(tmp_path)
    f.px["Zpump"] = 3.0
    f.hot_rows = [dict(mint="Zpump")]
    r.observe_decisions([])
    assert [v["src"] for v in r.pending.values()] == ["flow"]


def test_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ABC_LEARN", "0")
    r, f = rec(tmp_path)
    f.px["A"] = 1.0
    r.observe_decisions([dec("A")])
    assert not r.pending


def test_suspect_jump_flagged(tmp_path):
    r, f = rec(tmp_path)
    f.px["A"] = 1.0
    r._add("A", "gmgn", None, None, now=1000.0)
    f.px["A"] = 1e6
    r.label_tick(now=1010)
    assert next(iter(r.pending.values()))["suspect"] is True


def test_flow_top_only_pump_tokens(tmp_path):
    r, f = rec(tmp_path)
    f.px.update({"Xpump": 1.0, "Other": 1.0})
    f.hot_rows = [dict(mint="Other"), dict(mint="Xpump")]
    r.observe_decisions([])
    assert [v["mint"] for v in r.pending.values()] == ["Xpump"]


def test_pending_survives_restart(tmp_path):
    r, f = rec(tmp_path)
    f.px["A"] = 1.0
    r._add("A", "gmgn", None, None)
    r.save_pending()
    r2, f2 = rec(tmp_path)
    assert r2.load_pending() == 1
    s = next(iter(r2.pending.values()))
    assert s["mint"] == "A" and "gap" in s
    f2.px["A"] = 1.0
    assert r2._add("A", "gmgn", None, None) is False          # дедуп помнит восстановленный


def test_trending_window_from_cmd():
    import app as appmod
    assert appmod._trending_window_s("gmgn-cli market trending --interval 5m --limit 100") == 300
    assert appmod._trending_window_s("gmgn-cli market trending --limit 100") == 3600



class TestMidMarket:
    def mk(self, tmp_path, dex):
        f = FakeFlow()
        r = learn.Recorder(tmp_path, flow=f, dex_fn=lambda ms: {m: dex[m] for m in ms if m in dex}, sol_usd=lambda: 200.0)
        r.start = lambda: None
        return r, f

    def d(self, price=0.0002, mcap=300_000, liq=40_000):
        return dict(dex="pumpswap", price=price, liq=liq, mcap=mcap, vol_h1=1, vol_h24=1, buys_h1=1, sells_h1=1,
                    chg_h1=1, chg_h6=1, chg_h24=1, age_h=5.0)

    def test_records_with_dex_and_4h_horizon(self, tmp_path):
        r, f = self.mk(tmp_path, {"Apump": self.d()})
        f.px["Apump"] = 0.000001                               # ×200 $/SOL = $0.0002 = как у Dex
        f.mids = [("Apump", 1500.0, 200.0)]
        assert r.mid_scan(now=10_000.0) == 1
        s = next(iter(r.pending.values()))
        assert s["src"] == "mid" and s["horizon"] == learn.MID_HORIZON_S and s["dex"]["mcap"] == 300_000
        assert s["px_ratio"] == 1.0 and "suspect" not in s
        assert f.mid_args == (learn.MID_MIN_MCAP_USD / 200.0, learn.MID_MIN_LIQ_USD / 2 / 200.0)

    def test_price_mismatch_marked_suspect(self, tmp_path):
        r, f = self.mk(tmp_path, {"Apump": self.d(price=0.001)})   # Dex в 5 раз дороже потока
        f.px["Apump"] = 0.000001
        f.mids = [("Apump", 1500.0, 200.0)]
        r.mid_scan(now=10_000.0)
        assert next(iter(r.pending.values()))["suspect"] is True and r.stats["mid_price_mismatch"] == 1

    def test_dex_filters_small_and_farms(self, tmp_path):
        r, f = self.mk(tmp_path, {"Apump": self.d(mcap=150_000), "Bpump": self.d(liq=2_000)})
        f.px.update(Apump=0.000001, Bpump=0.000001)
        f.mids = [("Apump", 1500.0, 200.0), ("Bpump", 1500.0, 200.0), ("Cpump", 1500.0, 200.0)]
        assert r.mid_scan(now=10_000.0) == 0 and r.stats["mid_rejected_dex"] == 3

    def test_horizon_4h_labels_late_checkpoints(self, tmp_path):
        r, f = self.mk(tmp_path, {"Apump": self.d()})
        f.px["Apump"] = 0.000001
        f.mids = [("Apump", 1500.0, 200.0)]
        r.mid_scan(now=10_000.0)
        r.label_tick(now=10_000.0 + 3700)
        assert r.pending                                        # час прошёл — ещё размечается
        f.px["Apump"] = 0.0000013
        r.label_tick(now=10_000.0 + 14_401)
        assert not r.pending
        out = __import__("json").loads(next((tmp_path / "learn").glob("samples-*.jsonl")).read_text().splitlines()[0])
        assert out["at"]["14400"] == 0.3 and out["up"]["0.3"] == 14401

    def test_rate_limit_and_dex_error(self, tmp_path):
        r, f = self.mk(tmp_path, {})
        f.mids = [("Apump", 1500.0, 200.0)]
        r._dex_fn = lambda ms: (_ for _ in ()).throw(RuntimeError("429"))
        f.px["Apump"] = 0.000001
        r.mid_scan(now=10_000.0)
        assert r.stats["dex_errors"] == 1
        assert r.mid_scan(now=10_030.0) == 0 and r.stats["dex_errors"] == 1   # не чаще раза в минуту
