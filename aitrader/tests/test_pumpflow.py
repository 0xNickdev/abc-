"""pumpflow: разбор событий pump.fun/PumpSwap из настоящих protobuf Yellowstone, проверки
раскладки (mint в счетах tx, bool, время), окна/перевес/снайперы/дев/tracked, разрывы
покрытия, сверка с GMGN, доступ к /api/flow и подписка в том же стриме geyser."""
import base64
import pathlib
import struct
import sys
import time

import base58
import pytest

import geyser
import pumpflow as pf

sys.path.insert(0, str(pathlib.Path(geyser.__file__).resolve().parent / "ysproto"))
import geyser_pb2 as pb  # noqa: E402

MINT = "BzLtiUrd4ex7B8YydzxnbUmr6je1AKWhsKwtJR7ppump"
DEV = "Dev1111111111111111111111111111111111111111"
USERS = [base58.b58encode(bytes([i + 1]) * 32).decode() for i in range(10)]
POOL = base58.b58encode(b"\x77" * 32).decode()


def k(a: str) -> bytes:
    return base58.b58decode(a)


def trade_ev(mint=MINT, sol=10**9, tokens=10**12, is_buy=True, user=USERS[0], ts=None,
             vsol=40 * 10**9, vtok=800_000_000 * 10**6, creator=DEV, is_buy_byte=None) -> bytes:
    ts = int(ts or time.time())
    b = (k(mint) + struct.pack("<QQ", sol, tokens) + bytes([is_buy_byte if is_buy_byte is not None else int(is_buy)])
         + k(user) + struct.pack("<qQQQQ", ts, vsol, vtok, 10 * 10**9, 500 * 10**12)
         + b"\x00" * 32 + struct.pack("<QQ", 95, 1000) + k(creator) + struct.pack("<QQ", 5, 50))
    return pf.EV_TRADE + b


def create_ev(mint=MINT, creator=DEV, name="Dog", sym="DOG") -> bytes:
    def s(x):
        return struct.pack("<I", len(x)) + x.encode()
    body = (s(name) + s(sym) + s("ipfs://x") + k(mint) + b"\x05" * 32 + k(creator) + k(creator)
            + struct.pack("<q", int(time.time())) + b"\x00" * 40)
    return pf.EV_CREATE + body


def amm_ev(is_buy=True, base_amt=10**12, quote=2 * 10**9, user=USERS[1]) -> bytes:
    vals = [int(time.time()), base_amt, 0, 0, 0, 100_000_000 * 10**6, 80 * 10**9, quote] + [0] * 6
    return (pf.EV_AMM_BUY if is_buy else pf.EV_AMM_SELL) + struct.pack("<q13Q", *vals) + k(POOL) + k(user) + b"\x00" * 64


def tx_update(events: list[bytes], slot=100, keys_extra=(), via="cpi", amm_ix=False):
    """SubscribeUpdate(transaction) как с ноды: события — self-CPI инструкции или логи."""
    keys = [k(USERS[0]), k(pf.PUMP_PROGRAM), k(pf.AMM_PROGRAM), k(MINT), k(POOL), k(pf.SOL_MINT)] + [k(x) for x in keys_extra]
    u = pb.SubscribeUpdate()
    u.created_at.FromNanoseconds(time.time_ns() - 40_000_000)
    t = u.transaction
    t.slot = slot
    msg = t.transaction.transaction.message
    msg.account_keys.extend(keys)
    meta = t.transaction.meta
    outer = msg.instructions.add()
    if amm_ix:
        outer.program_id_index = 2
        outer.data = pf._disc("global", "buy") + b"\x00" * 16
        outer.accounts = bytes([4, 0, 0, 3, 5])                 # pool, user, cfg, base_mint, quote_mint
    else:
        outer.program_id_index = 1
        outer.data = pf._disc("global", "buy")
        outer.accounts = bytes([3, 0])
    if via == "cpi":
        ii = meta.inner_instructions.add()
        ii.index = 0
        for e in events:
            x = ii.instructions.add()
            x.program_id_index = 2 if e[:8] in (pf.EV_AMM_BUY, pf.EV_AMM_SELL) else 1
            x.data = pf.EVENT_IX_TAG + e
    else:
        meta.log_messages.extend(["Program log: Instruction: Buy"] + ["Program data: " + base64.b64encode(e).decode() for e in events])
    return u


@pytest.fixture
def flow(monkeypatch):
    f = pf.PumpFlow()
    f._started_at = time.time() - 4000                          # покрытие «весь час»
    f.stats["state"] = "on"
    monkeypatch.setattr(f, "_tracked_set", lambda: {USERS[5]: {"name": "whale"}})
    return f


class TestParse:
    def test_trade_layout(self):
        d = pf.parse_trade(trade_ev(sol=3 * 10**9, is_buy=False)[8:])
        assert d["mint"] == MINT and d["sol"] == 3 * 10**9 and d["is_buy"] is False
        assert d["user"] == USERS[0] and d["creator"] == DEV and d["vsol"] == 40 * 10**9

    def test_layout_guards(self):
        with pytest.raises(ValueError):
            pf.parse_trade(trade_ev(is_buy_byte=7)[8:])
        with pytest.raises(ValueError):
            pf.parse_trade(trade_ev(ts=1)[8:])                  # время из 1970 = сдвиг раскладки
        assert pf._decode(pf.EV_TRADE + b"\x00" * 20, False) == "bad"

    def test_create_and_amm(self):
        c = pf.parse_create(create_ev()[8:])
        assert (c["symbol"], c["mint"], c["creator"]) == ("DOG", MINT, DEV)
        a = pf.parse_amm(amm_ev(is_buy=False)[8:], False)
        assert a["pool"] == POOL and a["user"] == USERS[1] and a["sol"] == 2 * 10**9
        assert a["pool_quote"] == 78 * 10**9                    # резервы после продажи


class TestIngest:
    def test_cpi_and_logs_paths_and_metrics(self, flow):
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=USERS[1])], slot=100))          # снайпер в слоте создания
        flow.ingest(tx_update([trade_ev(user=USERS[2], sol=2 * 10**9)], slot=150, via="logs"))
        flow.ingest(tx_update([trade_ev(user=USERS[3], is_buy=False)], slot=151))
        flow.ingest(tx_update([trade_ev(user=DEV, is_buy=False, sol=5 * 10**8)], slot=152))
        m = flow.metrics(MINT, 300)
        assert (m["buys"], m["sells"]) == (2, 2) and m["buy_ratio"] == 0.5
        assert m["buy_vol_sol"] == 3.0 and m["unique_buyers"] == 2
        assert m["snipers"] == 1 and m["dev_sell_sol"] == 0.5 and m["dev_sells"] == 1
        assert m["symbol"] == "DOG" and m["mcap_sol"] == pytest.approx(50.0)
        assert m["full"] is True
        s = flow.snapshot()
        assert s["trades"] == 4 and s["creates"] == 1 and s["rejects"] == 0 and s["lag_ms"] >= 0

    def test_cpi_wins_over_logs_no_double_count(self, flow):
        u = tx_update([trade_ev()])
        u.transaction.transaction.meta.log_messages.append("Program data: " + base64.b64encode(trade_ev()).decode())
        flow.ingest(u)
        assert flow.metrics(MINT, 60)["buys"] == 1

    def test_mint_not_in_tx_is_rejected(self, flow):
        other = base58.b58encode(b"\x09" * 32).decode()
        flow.ingest(tx_update([trade_ev(mint=other)]))
        assert flow.metrics(other) is None and flow.stats["verify_fail"] == 1

    def test_garbage_counts_rejects(self, flow):
        flow.ingest(tx_update([pf.EV_TRADE + b"\x01" * 30]))
        assert flow.stats["rejects"] == 1 and flow.stats["trades"] == 0

    def test_amm_maps_pool_to_mint(self, flow):
        flow.ingest(tx_update([amm_ev(True, quote=2 * 10**9)], amm_ix=True))
        flow.ingest(tx_update([amm_ev(False, quote=10**9, user=USERS[2])], amm_ix=True))
        m = flow.metrics(MINT, 60)
        assert m["venue"] == "amm" and m["complete"] is True
        assert (m["buys"], m["sells"], m["vol_buy_ratio"]) == (1, 1, pytest.approx(2 / 3, abs=1e-3))

    def test_amm_reversed_sol_base_pool(self, flow):
        flow._pools[POOL] = (pf.SOL_MINT, MINT)            # SOL первым: buy base = продажа токена
        flow.ingest(tx_update([amm_ev(True, base_amt=3 * 10**9, quote=10**12)]))
        m = flow.metrics(MINT, 60)
        assert (m["buys"], m["sells"], m["sell_vol_sol"]) == (0, 1, 3.0)

    def test_amm_pool_from_token_balances(self, flow):
        u = tx_update([amm_ev(True)])
        meta = u.transaction.transaction.meta
        for mint in (MINT, pf.SOL_MINT):
            tb = meta.post_token_balances.add()
            tb.owner, tb.mint = POOL, mint
        flow.ingest(u)
        assert flow.metrics(MINT, 60)["buys"] == 1 and flow.stats["unmapped_pool"] == 0

    def test_non_sol_curve_is_not_a_failure(self, flow):
        flow.ingest(tx_update([trade_ev(sol=0, vsol=0)]))
        assert flow.stats["non_sol"] == 1 and flow.stats["verify_fail"] == 0

    def test_amm_event_without_known_pool(self, flow):
        flow.ingest(tx_update([amm_ev()], amm_ix=False))
        assert flow.stats["unmapped_pool"] == 1

    def test_tracked_wallet_in_and_out(self, flow):
        flow.ingest(tx_update([trade_ev(user=USERS[5], tokens=100)], keys_extra=[USERS[5]]))
        assert flow.metrics(MINT)["tracked_names"] == ["whale"]
        flow.ingest(tx_update([trade_ev(user=USERS[5], tokens=100, is_buy=False)]))
        assert flow.metrics(MINT)["tracked_in"] == 0

    def test_windows_and_hot(self, flow, monkeypatch):
        t = [time.time() - 1000]
        monkeypatch.setattr(pf.time, "time", lambda: t[0])
        for i in range(6):
            flow.ingest(tx_update([trade_ev(user=USERS[i])]), recv=t[0])
        t[0] += 900
        flow.ingest(tx_update([trade_ev(user=USERS[7], is_buy=False)]), recv=t[0])
        assert flow.metrics(MINT, 60)["buys"] == 0 and flow.metrics(MINT, 3600)["buys"] == 6
        assert flow.hot(3600, min_buyers=5)[0]["mint"] == MINT
        assert flow.hot(60, min_buyers=5) == []

    def test_gap_marks_windows_incomplete(self, flow, monkeypatch):
        t = [time.time()]
        monkeypatch.setattr(pf.time, "time", lambda: t[0])
        flow.ingest(tx_update([trade_ev()]), recv=t[0])
        t[0] += 120                                             # стрим молчал 2 мин
        flow.ingest(tx_update([trade_ev()]), recv=t[0])
        assert flow.stats["gaps"] == 1 and flow.metrics(MINT, 3600)["full"] is False

    def test_gc_drops_idle(self, flow):
        flow.ingest(tx_update([trade_ev()]))
        flow._m[MINT].last_ts -= pf.KEEP_S + 5
        flow._gc(time.time())
        assert flow.metrics(MINT) is None

    def test_queue_full_drops_not_blocks(self, flow, monkeypatch):
        monkeypatch.setattr(flow, "_q", pf.queue.Queue(maxsize=1))
        monkeypatch.setattr(flow, "_thread", object())
        flow.submit(1)
        flow.submit(2)
        assert flow.stats["dropped"] == 1


class TestCompare:
    def test_pairs_summary_and_journal(self, flow, tmp_path):
        for i in range(4):
            flow.ingest(tx_update([trade_ev(user=USERS[i])]))
        flow.ingest(tx_update([trade_ev(user=USERS[6], is_buy=False)]))
        c = pf.GmgnCompare(flow)
        path = tmp_path / "fc.jsonl"
        rows = [dict(address=MINT, symbol="DOG", buys=8, sells=2, volume=1000), dict(address="Unknown")]
        c.observe(rows, path)
        c.observe(rows, path)                                   # тот же токен — в журнал не чаще раза в 10 мин
        s = c.summary()
        assert s["trending_seen"] == 4 and s["in_flow"] == 2 and s["coverage"] == 0.5
        assert s["buy_ratio_abs_diff_med"] == pytest.approx(0.0) and s["gate65_agree"] == 1.0
        assert s["trades_flow_to_gmgn_med"] == 0.5
        assert len(path.read_text().splitlines()) == 1


class TestGeyserWiring:
    def test_request_includes_flow_filter(self, monkeypatch):
        monkeypatch.delenv("ABC_FLOW", raising=False)
        req = geyser.GeyserFeed()._request()
        f = req.transactions["flow"]
        assert set(f.account_include) == {pf.PUMP_PROGRAM, pf.AMM_PROGRAM} and f.vote is False and f.failed is False

    def test_flow_off(self, monkeypatch):
        monkeypatch.setenv("ABC_FLOW", "0")
        assert "flow" not in geyser.GeyserFeed()._request().transactions

    def test_tx_update_goes_to_flow_queue(self, monkeypatch):
        got = []
        monkeypatch.setattr(pf.FLOW, "submit", got.append)
        u = tx_update([trade_ev()])
        geyser.GeyserFeed()._handle(u)
        assert got == [u]


@pytest.mark.realauth
def test_api_flow_admin_only(tmp_path, monkeypatch):
    import app as appmod
    from test_app import _mu_client, _wallet_auth
    c = _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod, "PUBLIC_DEPLOY", True)
    monkeypatch.setattr(appmod, "ADMIN_MODE", False)
    assert c.get("/api/flow").status_code == 403
    pk, hdr = _wallet_auth(c)
    monkeypatch.setattr(appmod, "ADMIN_WALLETS", frozenset({pk}))
    r = c.get("/api/flow?window_s=60", headers=hdr)
    assert r.status_code == 200 and {"stats", "compare", "hot"} <= set(r.json())


def test_refused_flow_subscription_falls_back_to_positions_only(monkeypatch):
    """Тариф не пускает tx-подписку → следующий коннект без потока, цены позиций живут."""
    import grpc

    class Refused(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.PERMISSION_DENIED

    feed = geyser.GeyserFeed()
    reqs = []

    class Stub:
        def __init__(self, ch):
            pass

        def Subscribe(self, it):
            reqs.append(next(it))
            if len(reqs) == 1:
                raise Refused()
            feed._stop.set()
            return iter(())

    import geyser_pb2_grpc
    monkeypatch.setattr(geyser_pb2_grpc, "GeyserStub", Stub)
    monkeypatch.setattr(grpc, "secure_channel", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(feed._stop, "wait", lambda t: None)
    monkeypatch.delenv("ABC_FLOW", raising=False)
    feed._loop()
    assert "flow" in reqs[0].transactions and "flow" not in reqs[1].transactions
    assert "flow_blocked" in feed.snapshot()


def test_entry_attrib_gets_flow_snapshot(flow, monkeypatch):
    """BUY бота несёт ончейн-срез потока (30с/1м/5м, дев, снайперы) — топливо разбора сливов."""
    import app as appmod
    flow.ingest(tx_update([create_ev()], slot=100))
    flow.ingest(tx_update([trade_ev(user=USERS[1])], slot=100))
    flow.ingest(tx_update([trade_ev(user=DEV, is_buy=False, sol=2 * 10**9)], slot=103))
    monkeypatch.setattr(appmod.pumpflow, "FLOW", flow)
    at = {}
    appmod._enrich_flow(at, MINT)
    f = at["flow"]
    assert f["snipers"] == 1 and f["dev_sell_sol"] == 2.0 and f["30s"]["b"] == 1 and f["5m"]["s"] == 1
    other = {}
    appmod._enrich_flow(other, "NoSuchMint1111")
    assert other == {}


class TestDevSequence:
    def test_shakeout_sell_then_rebuy(self, flow, monkeypatch):
        t = [time.time()]
        monkeypatch.setattr(pf.time, "time", lambda: t[0])
        flow.ingest(tx_update([create_ev()], slot=100), recv=t[0])
        flow.ingest(tx_update([trade_ev(user=DEV, sol=10**9, tokens=1000)], slot=100), recv=t[0])
        t[0] += 5
        flow.ingest(tx_update([trade_ev(user=DEV, is_buy=False, sol=2 * 10**9, tokens=1000)], slot=110), recv=t[0])
        m = flow.metrics(MINT)
        assert m["dev_hold_share"] == 0 and m["dev_rebuys"] == 0 and m["dev_last_sell_ago_s"] == 0
        t[0] += 25
        flow.ingest(tx_update([trade_ev(user=DEV, sol=5 * 10**8, tokens=600)], slot=170), recv=t[0])
        m = flow.metrics(MINT)
        assert m["dev_rebuys"] == 1 and m["dev_rebuy_sol"] == 0.5 and m["dev_rebuy_delay_s"] == 25
        assert m["dev_hold_share"] == pytest.approx(600 / 1600, abs=1e-3) and m["dev_last_sell_ago_s"] == 25

    def test_dev_supply_share(self, flow):
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=50_000_000 * 10**6)], slot=100))     # 5% саплая
        flow.ingest(tx_update([trade_ev(user=DEV, is_buy=False, tokens=20_000_000 * 10**6)], slot=105))
        m = flow.metrics(MINT)
        assert m["dev_supply_pct"] == 3.0 and m["dev_supply_peak_pct"] == 5.0

    def test_dev_supply_unknown_without_create(self, flow):
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=10**12)]))
        assert flow.metrics(MINT)["dev_supply_pct"] is None

    def test_dev_buys_before_any_sell_are_not_rebuys(self, flow):
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=10)], slot=100))
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=10)], slot=101))
        m = flow.metrics(MINT)
        assert m["dev_rebuys"] == 0 and m["dev_hold_share"] == 1.0 and m["dev_last_sell_ago_s"] is None


class TestDumpCapacity:
    def test_curve_math(self):
        assert pf.PumpFlow.dump_impact(1000, 0) == 0.0
        assert pf.PumpFlow.dump_impact(1000, 1000) == pytest.approx(0.75)      # резерв ×2 → цена /4

    def test_dump_from_holders(self, flow):
        tok = 10**6 * 10**6                                                   # 1M токенов
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=30 * tok, vtok=800 * tok)], slot=100))
        flow.ingest(tx_update([trade_ev(user=USERS[1], tokens=20 * tok, vtok=780 * tok)], slot=101))  # снайпер
        flow.ingest(tx_update([trade_ev(user=USERS[2], tokens=10 * tok, vtok=770 * tok)], slot=150))
        m = flow.metrics(MINT)
        R = 770 * tok
        assert m["dump_dev_pct"] == pytest.approx(100 * pf.PumpFlow.dump_impact(R, 30 * tok), abs=0.1)
        assert m["dump_snipers_pct"] == pytest.approx(100 * pf.PumpFlow.dump_impact(R, 20 * tok), abs=0.1)
        assert m["dump_top10_pct"] == pytest.approx(100 * pf.PumpFlow.dump_impact(R, 60 * tok), abs=0.1)
        assert m["top10_supply_pct"] == 6.0 and m["top1_supply_pct"] == 3.0

    def test_unknown_without_full_history(self, flow):
        flow.ingest(tx_update([trade_ev()]))
        assert flow.metrics(MINT)["dump_top10_pct"] is None


def test_bot_skips_high_dump_capacity():
    import bot

    def d(addr, dump):
        return dict(decision=dict(action="ACTION", address=addr, size_sol=0.2, abc=dict(triggered=True, score=1),
                                  features=dict(buy_ratio=0.7, dump_top10_pct=dump)))
    out = bot.select_entries([d("A", 80.0), d("B", 30.0), d("C", None)], set(), 0, 10, 5, max_dump_pct=50)
    assert [a for a, _, _ in out] == ["B", "C"]


def test_price_from_main_pool_not_last_trade(flow):
    """13.10: у токена два пула (главный и мелкий чужой) — цена не прыгает на мелкий."""
    big, small = POOL, base58.b58encode(b"\x55" * 32).decode()
    flow._pools[big] = (MINT, pf.SOL_MINT)
    flow._pools[small] = (MINT, pf.SOL_MINT)

    def ev(pool, sol_res, tok_res):
        vals = [int(time.time()), 10**6, 0, 0, 0, tok_res, sol_res, 10**8] + [0] * 6
        return pf.EV_AMM_BUY + struct.pack("<q13Q", *vals) + k(pool) + k(USERS[1]) + b"\x00" * 64
    flow.ingest(tx_update([ev(big, 100 * 10**9, 10**15)], keys_extra=[small]))       # 100 SOL / 1e9 ток
    p_big = flow.metrics(MINT)["price_sol"]
    flow.ingest(tx_update([ev(small, 1 * 10**9, 10**12)], keys_extra=[small]))       # 1 SOL / 1e6 ток: ×10 дороже
    assert flow.metrics(MINT)["price_sol"] == pytest.approx(p_big, rel=0.01)


class TestShapeAndDeployer:
    def _clock(self, monkeypatch):
        t = [1_800_000_000.0]
        monkeypatch.setattr(pf.time, "time", lambda: t[0])
        return t

    def test_crash_drawdown_launch_spike(self, flow, monkeypatch):
        t = self._clock(monkeypatch)
        flow.ingest(tx_update([create_ev()], slot=100), recv=t[0])
        tok = 10**6 * 10**6
        for dt, vsol in ((1, 30), (5, 90), (20, 100), (70, 40), (130, 60)):   # SOL в кривой → цена
            t[0] += dt if dt < 20 else 0
            flow.ingest(tx_update([trade_ev(user=USERS[1], vsol=vsol * 10**9, vtok=800 * tok, ts=int(t[0]))], slot=101),
                        recv=1_800_000_000.0 + dt)
        m = flow.metrics(MINT, 3600)
        assert m["launch_x10s"] == pytest.approx(3.0)                 # 30 → 90 за первые 10 с
        assert m["dd_from_high_pct"] == pytest.approx(40.0)           # 100 → 60
        assert m["crash_1m_max_pct"] == pytest.approx(60.0)           # 100 → 40 на стыке минут

    def test_holder_basis(self, flow):
        tok = 10**6 * 10**6
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=USERS[1], sol=10**9, tokens=100 * tok, vsol=40 * 10**9, vtok=800 * tok)], slot=101))
        m = flow.metrics(MINT)
        price = 40 / 800e6
        basis = 1 / 100e6
        assert m["top25_profit_x"] == pytest.approx(price / basis, rel=0.01)
        flow.ingest(tx_update([trade_ev(user=USERS[1], is_buy=False, sol=10**9, tokens=50 * tok)], slot=102))
        assert flow._m[MINT].cost[USERS[1]] == 5 * 10**8           # половину продал — себестоимость пополам

    def test_deployer_history_survives_gc_and_restart(self, flow, tmp_path, monkeypatch):
        monkeypatch.setenv("ABC_DATA_DIR", str(tmp_path))
        flow.ingest(tx_update([create_ev()], slot=100))
        tok = 10**6 * 10**6
        flow.ingest(tx_update([trade_ev(user=DEV, tokens=50 * tok, vsol=80 * 10**9)], slot=100))
        flow.ingest(tx_update([trade_ev(user=DEV, is_buy=False, tokens=50 * tok, vsol=10 * 10**9)], slot=110))
        flow._m[MINT].last_ts -= pf.KEEP_S + 5
        flow._gc(time.time())
        h = flow.creator_history(DEV)
        assert h["dev_prev_tokens"] == 1 and h["dev_prev_rugs"] == 1 and h["dev_prev_migrated"] == 0
        flow.save_creators()
        f2 = pf.PumpFlow()
        f2.load_creators()
        assert f2.creator_history(DEV)["dev_prev_rugs"] == 1

    def test_snapshot_without_creator(self, flow):
        flow.ingest(tx_update([trade_ev()]))
        flow._m[MINT].creator = ""
        snap = flow.entry_snapshot(MINT)
        assert snap is not None and snap["dev_prev_tokens"] is None


class TestOrganicAndLiquidity:
    def test_wash_share_and_top_traders(self, flow):
        flow.ingest(tx_update([create_ev()], slot=100))
        flow.ingest(tx_update([trade_ev(user=USERS[1], sol=10**9)], slot=101))           # круг туда-обратно
        flow.ingest(tx_update([trade_ev(user=USERS[1], is_buy=False, sol=10**9)], slot=102))
        flow.ingest(tx_update([trade_ev(user=USERS[2], sol=2 * 10**9)], slot=103))       # органичный покупатель
        m = flow.metrics(MINT)
        assert m["wash_share_5m"] == 0.5 and m["holders_obs"] == 1
        assert m["top3_trader_share_5m"] == 1.0

    def test_lp_mc_ratio_and_turnover(self, flow):
        flow._pools[POOL] = (MINT, pf.SOL_MINT)
        vals = [int(time.time()), 10**6, 0, 0, 0, 100_000_000 * 10**6, 80 * 10**9, 10**9] + [0] * 6
        ev = pf.EV_AMM_BUY + struct.pack("<q13Q", *vals) + k(POOL) + k(USERS[1]) + b"\x00" * 64
        flow.ingest(tx_update([ev]))
        m = flow.metrics(MINT)
        mcap = m["price_sol"] * pf.PUMP_SUPPLY
        assert m["lp_mc_ratio"] == pytest.approx(2 * m["liq_sol"] / mcap, rel=1e-3)
        assert m["turnover_1h"] == pytest.approx(1.0 / mcap, abs=1e-3)


def test_canonical_pool_flag(flow):
    canon = pf.canonical_pool(MINT)
    for pool, sol in ((canon, 100), (POOL, 10)):
        flow._pools[pool] = (MINT, pf.SOL_MINT)
        vals = [int(time.time()), 10**6, 0, 0, 0, 10**15, sol * 10**9, 10**8] + [0] * 6
        ev = pf.EV_AMM_BUY + struct.pack("<q13Q", *vals) + k(pool) + k(USERS[1]) + b"\x00" * 64
        flow.ingest(tx_update([ev], keys_extra=[canon]))
    m = flow.metrics(MINT)
    assert m["pool_canonical"] is True and m["pool_count"] == 2
    flow._m[MINT].pool_px[POOL] = (1.0, 10**12, 10**15, time.time())     # чужой пул стал главным
    assert flow.metrics(MINT)["pool_canonical"] is False


class TestProTraders:
    def round_trip(self, flow, user, buy_sol, sell_sol, slot):
        tok = 10**12
        flow.ingest(tx_update([trade_ev(user=user, sol=buy_sol, tokens=tok)], slot=slot))
        flow.ingest(tx_update([trade_ev(user=user, is_buy=False, sol=sell_sol, tokens=tok)], slot=slot + 1))

    def test_ledger_realized_pnl_and_wins(self, flow):
        self.round_trip(flow, USERS[3], 10**9, 3 * 10**9, 200)          # +2 SOL
        self.round_trip(flow, USERS[3], 10**9, 5 * 10**8, 210)          # −0.5 SOL
        led = flow._wallets[USERS[3]]
        assert led[0] == int(1.5e9) and led[1] == 2 and led[2] == 1

    def test_sell_of_tokens_bought_before_us_is_ignored(self, flow):
        flow.ingest(tx_update([trade_ev(user=USERS[4], is_buy=False, sol=10**9, tokens=10**12)], slot=5))
        assert USERS[4] not in flow._wallets

    def test_pro_detection_and_features(self, flow, monkeypatch):
        monkeypatch.setattr(pf, "PRO_MIN_CLOSED", 2)
        for i in range(3):
            self.round_trip(flow, USERS[5], 10**9, 3 * 10**9, 300 + 10 * i)
        assert flow.is_pro(USERS[5]) and flow.pro_stats()[0]["pnl_sol"] == 6.0
        flow.ingest(tx_update([create_ev()], slot=400))
        flow.ingest(tx_update([trade_ev(user=USERS[5], tokens=10**12)], slot=401))
        m = flow.metrics(MINT)
        assert m["pro_in"] == 1 and m["pro_buys_5m"] >= 1

    def test_wallets_persist(self, flow, tmp_path, monkeypatch):
        monkeypatch.setenv("ABC_DATA_DIR", str(tmp_path))
        for i in range(2):
            self.round_trip(flow, USERS[6], 10**9, 2 * 10**9, 500 + 10 * i)
        flow.save_wallets()
        f2 = pf.PumpFlow()
        f2.load_wallets()
        assert f2._wallets[USERS[6]][1] == 2


def test_ledger_skips_sells_beyond_observed_holdings(flow):
    """16.10: арбитражник продаёт больше, чем мы видели купленным (токены мимо потока) — не в PnL."""
    flow.ingest(tx_update([trade_ev(user=USERS[7], sol=10**9, tokens=10**12)], slot=10))
    flow.ingest(tx_update([trade_ev(user=USERS[7], is_buy=False, sol=10**9, tokens=3 * 10**12)], slot=11))
    assert USERS[7] not in flow._wallets
