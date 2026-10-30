"""ZOOAI-фильтр входа (30.10): шансы роста/дев-дамп из zooaiagency.com; гейт режет avoid/низкий run;
fail-open при ошибке API; в тени не блокирует."""
import zooai


def test_block_reason_logic(monkeypatch):
    monkeypatch.setattr(zooai, "MIN_RUN", 0.03)
    monkeypatch.setattr(zooai, "MIN_VS_AVG", 0)
    monkeypatch.setattr(zooai, "MAX_DEVDUMPS", 0.6)
    assert zooai.block_reason(None) is None                               # API молчит — не режем
    assert zooai.block_reason(dict(verdict="avoid")) == "zooai avoid"
    assert zooai.block_reason(dict(proceed=False)) == "zooai avoid"
    assert zooai.block_reason(dict(run=0.01)).startswith("zooai run")     # ниже порога роста
    assert zooai.block_reason(dict(run=0.10, dev_dumps=0.8)).startswith("zooai devDumps")
    assert zooai.block_reason(dict(run=0.10, dev_dumps=0.2)) is None      # прошёл
    assert zooai.block_reason(dict(run=None, dev_dumps=None)) is None     # нет данных — не режем


def test_vsavg_rescues_low_run(monkeypatch):
    monkeypatch.setattr(zooai, "MIN_RUN", 0.05)
    monkeypatch.setattr(zooai, "MIN_VS_AVG", 2.0)
    assert zooai.block_reason(dict(run=0.03, vs_average=3.6)) is None     # ниже run, но 3.6× среднего
    assert zooai.block_reason(dict(run=0.03, vs_average=1.2)).startswith("zooai run")


def test_check_parses_and_caches(monkeypatch):
    import httpx
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        class R:
            def raise_for_status(self): pass
            def json(self): return dict(verdict="caution", proceed=True,
                                        odds=dict(run=0.097, vsAverage=3.6, devDumps=None),
                                        reasons=["creator sold 100%"])
        return R()
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(zooai, "_cache", {})
    z = zooai.check("Mint1")
    assert z["run"] == 0.097 and z["vs_average"] == 3.6 and z["verdict"] == "caution"
    zooai.check("Mint1")                                                  # второй раз — из кэша
    assert len(calls) == 1


def test_check_fail_open(monkeypatch):
    import httpx
    def boom(url, **kw): raise RuntimeError("down")
    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(zooai, "_cache", {})
    assert zooai.check("Mint2") is None                                   # ошибка → None (не блокируем)


def test_cabal_and_wallet_gates(monkeypatch):
    monkeypatch.setattr(zooai, "MIN_RUN", 0)
    monkeypatch.setattr(zooai, "MAX_DEVDUMPS", 0)
    monkeypatch.setattr(zooai, "MAX_DUMPED48H", 3)
    monkeypatch.setattr(zooai, "MAX_BUNDLERS_HELD", 30)
    assert zooai.block_reason(dict(funder_cabal=True)) == "zooai cabal"
    assert zooai.block_reason(dict(dumped48h=4)).startswith("zooai dumped48h")
    assert zooai.block_reason(dict(dumped48h=1)) is None
    assert zooai.block_reason(dict(bundlers_held=45)).startswith("zooai bundlers")
    assert zooai.block_reason(dict(bundlers_held=10)) is None
    assert zooai.block_reason(dict(funder_cabal=False, dumped48h=1, bundlers_held=10)) is None


def test_check_extracts_cabal(monkeypatch):
    import httpx
    def fake_get(url, **kw):
        class R:
            def raise_for_status(self): pass
            def json(self): return dict(verdict="avoid", proceed=False, odds={},
                facts=dict(creatorDumped48h=5, bundlersHeldPct=40),
                funding=dict(funder="W", funderIsHot=False, otherCreators=3, theirDumps48h=12, funderLabel=None))
        return R()
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(zooai, "_cache", {})
    z = zooai.check("M")
    assert z["funder_cabal"] is True and z["dumped48h"] == 5 and z["bundlers_held"] == 40.0
