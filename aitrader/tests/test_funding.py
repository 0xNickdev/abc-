"""funding: источник денег держателей → инсайдеры (деньги от дева), бандлы (общий источник),
свежие кошельки; хабы-биржи не связь; лимит запросов; кэш на диске; ошибки RPC не роняют."""
import pytest

import funding as fd

DEV, DEVFUND, HUB = "Dev1", "DevFunder1", "Binance1"


def tx_transfer(src, dst):
    return {"transaction": {"message": {"instructions": [
        {"program": "system", "parsed": {"type": "transfer", "info": {"source": src, "destination": dst, "lamports": 10}}}]}},
            "meta": {"innerInstructions": []}}


def make(funders, old=(), tmp_path=None):
    """funders: wallet → кто прислал SOL; old — кошельки со «старой» историей."""
    calls = []

    def rpc(method, params):
        calls.append(method)
        w = params[0]
        if method == "getSignaturesForAddress":
            n = fd.FRESH_SIGS if w in old else 3
            return [{"signature": f"{w}-s{i}", "blockTime": 1000 + i} for i in range(n)]
        return tx_transfer(funders[w.split("-s")[0]], w.split("-s")[0])
    r = fd.FundingResolver(rpc=rpc, path=(tmp_path / "f.json") if tmp_path else None)
    r.start = lambda: None
    return r, calls


def test_find_funder_create_account():
    tx = {"transaction": {"message": {"instructions": []}}, "meta": {"innerInstructions": [{"instructions": [
        {"program": "system", "parsed": {"type": "createAccount", "info": {"source": "S", "newAccount": "W"}}}]}]}}
    assert fd._find_funder(tx, "W") == "S" and fd._find_funder({}, "W") is None


def test_insiders_bundle_fresh(tmp_path):
    funders = {DEV: DEVFUND, "a": DEV, "b": DEVFUND, "c": "X", "d": "X", "e": "X", "f": "Y"}
    r, _ = make(funders, old={"old1"}, tmp_path=tmp_path)
    r.request(list(funders) + ["old1"])
    while r.tick():
        pass
    f = r.features([("a", 5.0), ("b", 3.0), ("c", 2.0), ("d", 2.0), ("e", 1.0), ("f", 1.0), ("old1", 4.0), ("unk", 9.0)], DEV)
    assert f["insiders_n"] == 2 and f["insiders_pct"] == 8.0           # a — от дева, b — от источника дева
    assert f["bundle_max"] == 3 and f["bundle_pct"] == 5.0              # c, d, e — один источник X
    assert f["fresh_n"] == 6 and f["fund_known"] == 7 and f["fund_total"] == 8


def test_hub_is_not_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "HUB_FANOUT", 3)
    funders = {f"w{i}": HUB for i in range(5)}
    r, _ = make(funders, tmp_path=tmp_path)
    r.request(list(funders))
    while r.tick():
        pass
    f = r.features([(w, 1.0) for w in funders], None)
    assert r.is_hub(HUB) and f["bundle_max"] == 0 and f["bundle_pct"] == 0


def test_budget_and_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("ABC_FUNDING_RPM", "4")
    r, calls = make({"low": "L", "hi": "H"}, tmp_path=tmp_path)
    r.request(["low"], priority=5)
    r.request(["hi"], priority=1)
    assert r.tick() == 2 and len(calls) == 4
    assert r.tick() == 0                                                # бюджет минуты исчерпан
    assert list(r.cache) == ["hi", "low"]                               # кандидат бота первым


def test_cache_persist_and_errors(tmp_path):
    r, _ = make({"a": "A"}, tmp_path=tmp_path)
    r.request(["a"])
    r.tick()
    r.save()
    r2, calls = make({}, tmp_path=tmp_path)
    r2.load()
    assert r2.cache["a"][0] == "A"
    r2.request(["a"])                                                   # уже известен — без запросов
    assert r2.tick() == 0 and calls == []
    bad = fd.FundingResolver(rpc=lambda m, p: (_ for _ in ()).throw(RuntimeError("boom")), path=tmp_path / "b.json")
    bad.start = lambda: None
    bad.request(["z"])
    assert bad.tick() == 0 and bad.stats["rpc_errors"] == 1


def test_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ABC_FUNDING", "0")
    r, _ = make({"a": "A"}, tmp_path=tmp_path)
    r.request(["a"])
    assert r._q == []
