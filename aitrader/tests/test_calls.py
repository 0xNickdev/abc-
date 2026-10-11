"""Сканер коллов: парсинг Telegram/X, валидация контрактов, рейтинг коллеров, лента."""
import pytest

import calls as cm

CA1 = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
CA2 = "HZL2ga4kXjZp8377B5TcWBuqvBTtxqK2KZb6mjcnpump"
import time as _time
NOW = _time.time()
TG_PAGE = f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="alphacalls/101">
<div class="tgme_widget_message_text js-message_text" dir="auto">New gem &#036;CAT <b>CA:</b> {CA1} aping</div>
<time datetime="2026-10-11T08:00:00+00:00" class="time">08:00</time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="alphacalls/102">
<div class="tgme_widget_message_text js-message_text">chart <a href="https://dexscreener.com/solana/{CA2}">here</a></div>
<time datetime="2026-10-11T09:00:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="alphacalls/103">
<div class="tgme_widget_message_text js-message_text">gm frens</div></div></div>
"""


@pytest.fixture
def sc(tmp_path, monkeypatch):
    prices = {CA1: dict(price=1.0, mcap=10000, symbol="CAT"), CA2: dict(price=2.0, mcap=50000, symbol="TES")}
    monkeypatch.setattr(cm, "_quotes", lambda cas: {c: dict(prices[c]) for c in cas if c in prices})
    s = cm.CallScanner(tmp_path)
    s._prices = prices
    return s


def test_parse_tg_extracts_posts_text_links_and_time():
    posts = cm.parse_tg(TG_PAGE, "alphacalls")
    assert [p["id"] for p in posts] == ["alphacalls/101", "alphacalls/102"]   # пост без CA пропущен
    assert posts[0]["cas"] == [CA1] and "$CAT" in posts[0]["text"]
    assert posts[1]["cas"] == [CA2]                                           # контракт из ссылки
    assert posts[0]["url"] == "https://t.me/alphacalls/101"
    assert posts[0]["ts"] < posts[1]["ts"]


def test_extract_cas_ignores_short_and_signatures():
    sig = "5" * 88
    assert cm.extract_cas(f"tx {sig} ca {CA1} again {CA1}") == [CA1]


def test_only_known_solana_tokens_are_recorded(sc):
    junk = "1111111111111111111111111111111111111111"
    n = sc._add("tg", "alphacalls", [dict(cas=[CA1, junk], ts=cm._now(), url="u", text="t")])
    assert n == 1 and sc.calls[0]["ca"] == CA1 and sc.calls[0]["mcap0"] == 10000


def test_duplicate_calls_are_not_counted_twice(sc):
    p = [dict(cas=[CA1], ts=NOW)]
    assert sc._add("tg", "alphacalls", p) == 1
    assert sc._add("tg", "alphacalls", p) == 0
    assert sc._add("tg", "othercalls", p) == 1       # другой канал — отдельный колл


def test_callers_rating_uses_peak_vs_call_price(sc):
    for ch in ("good", "bad"):
        sc._add("tg", ch, [dict(cas=[CA1], ts=NOW)])
    sc._prices[CA1]["price"] = 3.0                     # пик 3x от цены колла
    sc.update_prices()
    r = {x["name"]: x for x in sc.callers()}
    assert r["good"]["hit_rate"] == 1.0 and r["good"]["median_x"] == 3.0


def test_feed_groups_callers_by_token_and_reports_x(sc):
    sc._add("tg", "a", [dict(cas=[CA1], ts=NOW-200, url="u1")])
    sc._add("x", "", [dict(author="kol1", cas=[CA1], ts=NOW-100, url="u2")])
    sc._prices[CA1]["price"] = 1.5
    sc.update_prices()
    rows = sc.feed(hours=24)
    assert len(rows) == 1 and rows[0]["symbol"] == "CAT"
    assert [c["name"] for c in rows[0]["callers"]] == ["a", "kol1"]        # по времени колла
    assert rows[0]["x_now"] == 1.5 and rows[0]["peak_x"] == 1.5
    assert sc.feed(source="x")[0]["callers"][0]["name"] == "kol1"


def test_clean_lists_accept_links_and_at_names(sc):
    src = sc.set_sources(x=["@Ansem", "https://x.com/blknoiz06", "bad name!"],
                         tg=["https://t.me/s/alphacalls", "t.me/x", "alphacalls"])
    assert src["x"] == ["Ansem", "blknoiz06"]
    assert src["tg"] == ["alphacalls"]
    assert cm.CallScanner(sc.path.parent).sources == src                     # сохранено на диск


def test_top_discovered_callers_join_watched_list(sc, monkeypatch):
    monkeypatch.setattr(cm, "AUTO_MIN_CALLS", 1)
    sc._add("x", "", [dict(author="sharp", cas=[CA1], ts=NOW)], discovered=True)
    sc._prices[CA1]["price"] = 5.0
    sc.update_prices()
    sc.set_sources(x=["manual"])
    assert sc.watched_x() == ["manual", "sharp"]


def test_discover_is_once_per_token_and_rate_limited(sc, monkeypatch):
    hits = []
    monkeypatch.setattr(cm.xapi, "key", lambda: "k")
    monkeypatch.setattr(cm.xapi, "ca_mentions", lambda ca: hits.append(ca) or dict(authors=[dict(username="early")]))
    assert sc.discover(CA1) == 1
    assert sc.discover(CA1) == 0                       # тот же токен — не повторяем
    assert sc.discover(CA2) == 0                       # лимит: не чаще раза в 30 с
    assert hits == [CA1] and sc.calls[0]["discovered"] is True


def test_state_persists_across_restart(sc):
    sc._add("tg", "alphacalls", [dict(cas=[CA1], ts=NOW)])
    sc._save()
    again = cm.CallScanner(sc.path.parent)
    assert len(again.calls) == 1 and again.tokens[CA1]["symbol"] == "CAT"


def test_sources_file_edit_is_picked_up_live(sc):
    import json, os, time as _t
    sc.src_path.write_text(json.dumps(dict(x=["kolA"], tg=["chanA"])))
    os.utime(sc.src_path, (_t.time() + 5, _t.time() + 5))
    sc._load_sources()
    assert sc.sources == dict(x=["kolA"], tg=["chanA"])


def test_tg_polls_channels_round_robin(sc, monkeypatch):
    seen = []
    class R:
        status_code = 200
        text = ""
    monkeypatch.setattr(cm.httpx, "get", lambda url, **k: seen.append(url.rsplit("/", 1)[1]) or R())
    monkeypatch.setattr(cm, "TG_BATCH", 2)
    sc.set_sources(tg=["aaaa", "bbbb", "cccc"])
    sc.poll_tg(); sc.poll_tg()
    assert seen == ["aaaa", "bbbb", "cccc", "aaaa"]


def test_old_posts_are_not_counted_as_calls(sc):
    assert sc._add("tg", "alphacalls", [dict(cas=[CA1], ts=cm._now() - 3600)]) == 0
    assert sc._add("tg", "alphacalls", [dict(cas=[CA1], ts=cm._now() - 60)]) == 1
