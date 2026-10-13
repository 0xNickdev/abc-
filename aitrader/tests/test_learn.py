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
    assert s["up"] == {"0.2": 60, "0.5": 60} and s["dn"] == {"-0.2": 100, "-0.35": 100, "-0.5": 300}
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
