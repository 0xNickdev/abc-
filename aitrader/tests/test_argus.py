"""Argus: ачивки по уровням без повторов и без флуда старым, живые пики из потока с защитой
от чужого масштаба, сигнал нескольких проверенных коллеров, команды и отчёт по контракту."""
import time

import pytest

import argus as ag
import calls

CA = "BzLtiUrd4ex7B8YydzxnbUmr6je1AKWhsKwtJR7ppump"
CA2 = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


class FakeFlow:
    def __init__(self):
        self.m = {}

    def metrics(self, mint, window_s=300):
        return self.m.get(mint)


def call(author, ca=CA, ts=None, price0=1.0, mcap0=50_000, source="tg"):
    ts = ts or time.time()
    return dict(ca=ca, source=source, author=author, ts=ts, seen=ts, url=f"https://t.me/{author}/1",
                text="", price0=price0, mcap0=mcap0, discovered=False)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    sc = calls.CallScanner(tmp_path)
    flow = FakeFlow()
    a = ag.Argus(sc, tmp_path, flow=flow, sol_usd=lambda: 100.0)
    a.bot_user = "argus_abc_bot"
    sent = []
    monkeypatch.setattr(a, "send", lambda chat, text, preview=False: sent.append((chat, text)) or True)
    monkeypatch.setattr(a, "send_animation", lambda chat, anim, caption: sent.append((chat, caption)) or "FILEID")
    import cards
    monkeypatch.setattr(cards, "achievement_gif", lambda **k: b"GIF89a")
    monkeypatch.setenv("ARGUS_CHANNEL", "@argus_calls")
    a.sent_log = sent
    return a


def add(bot, *cs, peak=1.0, price_now=1.0, symbol="DOG"):
    for c in cs:
        bot.calls.calls.append(c)
        bot.calls.tokens[c["ca"]] = dict(symbol=symbol, peak=peak, price_now=price_now, mcap_now=50_000 * price_now)


class TestAchievements:
    def test_old_results_not_posted_on_first_pass(self, bot):
        add(bot, call("gemhunter"), peak=8.0)
        bot.engine_tick()
        assert bot.sent_log == [] and bot.st["primed"]

    def test_new_level_posted_once_highest_only(self, bot):
        add(bot, call("gemhunter"))
        bot.engine_tick()                                   # прайминг
        bot.calls.tokens[CA]["peak"] = 8.4
        bot.engine_tick()
        assert len(bot.sent_log) == 1
        chat, text = bot.sent_log[0]
        assert chat == "@argus_calls" and "x5" in text and "$50K → $420K" in text and CA in text
        bot.engine_tick()
        assert len(bot.sent_log) == 1                       # тот же уровень — не повторяем
        bot.calls.tokens[CA]["peak"] = 10.5
        bot.engine_tick()
        assert "x10" in bot.sent_log[-1][1]

    def test_subscribers_get_it_too(self, bot):
        bot.st["subs"] = [111]
        add(bot, call("gemhunter"))
        bot.engine_tick()
        bot.calls.tokens[CA]["peak"] = 2.2
        bot.engine_tick()
        assert {c for c, _ in bot.sent_log} == {"@argus_calls", 111}


class TestLivePeaks:
    def test_flow_price_updates_peak(self, bot):
        add(bot, call("a"), peak=1.0, price_now=1.0)
        bot.flow.m[CA] = dict(price_sol=0.03, last_trade_ago_s=2, buys=1, sells=0)   # ×100 USD = 3.0
        bot.engine_tick()
        t = bot.calls.tokens[CA]
        assert t["peak"] == pytest.approx(3.0) and t["price_now"] == pytest.approx(3.0)

    def test_foreign_scale_rejected(self, bot):
        add(bot, call("a"), peak=1.0, price_now=1.0)
        bot.flow.m[CA] = dict(price_sol=5.0, last_trade_ago_s=2)       # $500 — ×500: чужой масштаб
        bot.engine_tick()
        assert bot.calls.tokens[CA]["peak"] == 1.0

    def test_stale_flow_ignored(self, bot):
        add(bot, call("a"))
        bot.flow.m[CA] = dict(price_sol=0.03, last_trade_ago_s=600)
        bot.engine_tick()
        assert bot.calls.tokens[CA]["peak"] == 1.0


class TestSignals:
    def _history(self, bot, names):
        """Каждому коллеру — 3 прошлых колла с x2+ (хитрейт 100%)."""
        old = time.time() - 40 * 3600
        for i, n in enumerate(names):
            for j in range(3):
                ca = f"Old{i}{j}".ljust(43, "1")
                add(bot, call(n, ca=ca, ts=old), peak=3.0)

    def test_three_proven_callers_fire_once(self, bot):
        self._history(bot, ["a", "b", "c"])
        bot.engine_tick()
        bot.sent_log.clear()
        now = time.time()
        add(bot, call("a", ca=CA2, ts=now - 600), call("b", ca=CA2, ts=now - 300), call("c", ca=CA2, ts=now))
        bot.engine_tick()
        sig = [t for _, t in bot.sent_log if "ARGUS SIGNAL" in t]
        assert len(sig) == 1 and "3 proven callers" in sig[0]          # в канал, подписчиков нет
        bot.engine_tick()
        assert len([t for _, t in bot.sent_log if "ARGUS SIGNAL" in t]) == 1

    def test_unproven_callers_dont_count(self, bot):
        bot.engine_tick()
        now = time.time()
        add(bot, call("x1", ca=CA2, ts=now), call("x2", ca=CA2, ts=now), call("x3", ca=CA2, ts=now))
        bot.engine_tick()
        assert not any("ARGUS SIGNAL" in t for _, t in bot.sent_log)


class TestCommands:
    def msg(self, text, chat=5, kind="private"):
        return dict(chat=dict(id=chat, type=kind), text=text)

    def test_alerts_subscribe_and_unsubscribe(self, bot):
        bot.handle(self.msg("/alerts on"))
        assert bot.st["subs"] == [5]
        bot.handle(self.msg("/alerts off"))
        assert bot.st["subs"] == []

    def test_contract_report(self, bot):
        add(bot, call("gemhunter"), peak=4.0)
        bot.flow.m[CA] = dict(buys=30, sells=10, buy_ratio=0.75, unique_buyers=22, net_flow_sol=12.5,
                              snipers=3, dev_sell_sol=0, tracked_in=2, price_sol=0.0001, mcap_sol=100,
                              venue="curve", curve_sol=40.0, age_s=600, dev_sells=0, symbol="DOG")
        bot.handle(self.msg(f"what about {CA} ?"))
        text = bot.sent_log[-1][1]
        assert "$DOG" in text and "buys 75%" in text and "2 smart wallets in" in text
        assert "Called by 1" in text and "peak x4.0" in text and "$10K" in text

    def test_top_hot_caller_and_deeplink(self, bot):
        for i in range(3):
            add(bot, call("gemhunter", ca=f"Tok{i}".ljust(43, "1")), peak=2.5)
        bot.handle(self.msg("/top"))
        assert "gemhunter" in bot.sent_log[-1][1] and "100% hit" in bot.sent_log[-1][1]
        bot.handle(self.msg("/hot"))
        assert "x2.5" in bot.sent_log[-1][1]
        bot.handle(self.msg("/start c_tg_gemhunter"))
        assert "3 calls" in bot.sent_log[-1][1]

    def test_html_is_escaped(self, bot):
        add(bot, call("<b>evil</b>"), symbol="<script>")
        bot.handle(self.msg(f"{CA}"))
        assert "<script>" not in bot.sent_log[-1][1] and "&lt;script&gt;" in bot.sent_log[-1][1]


def test_disabled_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_BOT_TOKEN", raising=False)
    a = ag.Argus(calls.CallScanner(tmp_path), tmp_path)
    a.start()
    assert a._threads == [] and a.snapshot()["state"] == "off"


def test_formatters():
    assert ag.usd(62_000) == "$62K" and ag.usd(500_000) == "$500K" and ag.usd(1_250_000) == "$1.2M"
    assert ag.dur(8040) == "2h 14m" and ag.xfmt(8.4) == "x8.4" and ag.xfmt(12) == "x12"


def test_card_rendered_once_then_file_id(tmp_path, monkeypatch):
    import cards
    a = ag.Argus(calls.CallScanner(tmp_path), tmp_path)
    monkeypatch.setenv("ARGUS_CHANNEL", "@ch")
    a.st["subs"] = [1, 2]
    renders, sent = [], []
    monkeypatch.setattr(cards, "achievement_gif", lambda **k: renders.append(k) or b"GIF89a")
    monkeypatch.setattr(a, "send_animation", lambda chat, anim, cap: sent.append((chat, anim)) or "FID")
    monkeypatch.setattr(a, "send", lambda *x, **k: pytest.fail("text fallback not expected"))
    a.broadcast(dict(text="hi", card=dict(level=5)))
    assert len(renders) == 1 and sent == [("@ch", b"GIF89a"), (1, "FID"), (2, "FID")]


def test_card_failure_falls_back_to_text(tmp_path, monkeypatch):
    import cards
    a = ag.Argus(calls.CallScanner(tmp_path), tmp_path)
    monkeypatch.setenv("ARGUS_CHANNEL", "@ch")
    texts = []
    monkeypatch.setattr(cards, "achievement_gif", lambda **k: (_ for _ in ()).throw(OSError("no font")))
    monkeypatch.setattr(a, "send", lambda chat, text, preview=False: texts.append(chat) or True)
    a.broadcast(dict(text="hi", card=dict(level=5)))
    assert texts == ["@ch"]


def test_real_card_renders():
    import cards
    g = cards.achievement_gif(level=5, mult=8.1, symbol="DOG", caller="@x", mc0=62000, mc_peak=500000,
                              took="2h 14m", seed="abc")
    assert g[:6] == b"GIF89a" and 50_000 < len(g) < 2_000_000
