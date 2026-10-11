"""Карточка проверки токена (Signals → Safety): статусы, вердикт, кэш и лимиты."""
import pytest

import app as appmod
from test_app import _mu_client

ADDR = "TokenCheckAddrxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
CLEAN = dict(honeypot=False, renounced_mint=True, renounced_freeze=True, sell_tax=0.0, buy_tax=0.0,
             top10=0.2, dev_hold=0.0, rug_ratio=0.05, bundler=0.05, sniper_count=2)


@pytest.fixture
def client(tmp_path, monkeypatch):
    c = _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod, "_CHECK_CACHE", {})
    monkeypatch.setattr(appmod, "_CHECK_RECENT", [])
    return c


def _by(items):
    return {i["key"]: i for i in items}


def test_clean_token_looks_clean():
    items = appmod.check_items(CLEAN, None)
    assert all(i["status"] == "ok" for i in items)
    assert appmod.check_verdict(items)["label"] == "Looks clean"


def test_red_flags_make_it_risky():
    f = dict(CLEAN, renounced_freeze=False, bundler=0.45, dev_hold=0.2)
    items = _by(appmod.check_items(f, None))
    assert items["freeze"]["status"] == "bad" and items["bundles"]["status"] == "bad"
    assert items["dev_hold"]["status"] == "bad"
    v = appmod.check_verdict(list(items.values()))
    assert v["level"] == "bad" and v["label"] == "Risky"


def test_two_warnings_mean_caution():
    items = appmod.check_items(dict(CLEAN, top10=0.4, sniper_count=10), None)
    assert appmod.check_verdict(items)["label"] == "Caution"


def test_deep_x_checks():
    deep = dict(handle="shill", x_available=True, holders=dict(ok=True, insiders=0, red_flag=False),
                x=dict(ok=True, account_age_days=2, followers=10, other_tokens=5),
                renames=dict(ok=True, renames=4, red_flag=True),
                timeline=dict(ok=True, mentions=6, posted_before_official=True, first_by_official=False))
    items = _by(appmod.check_items(CLEAN, deep))
    assert items["x_account"]["status"] == "bad"          # аккаунт 2 дня
    assert items["x_reuse"]["status"] == "bad"            # 5 других токенов
    assert items["x_renames"]["status"] == "bad"
    assert items["x_first"]["status"] == "warn"
    assert items["insiders"]["value"] == "0"


def test_no_x_link_is_a_warning():
    items = _by(appmod.check_items(CLEAN, dict(handle=None, x_available=True)))
    assert items["x_linked"]["status"] == "warn"


def test_endpoint_caches_deep_check_across_users(client, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.dexadapter, "token_twitter", lambda a: calls.append(a) or "")
    monkeypatch.setattr(appmod.rugcheck, "check", lambda a: dict(ok=True, insiders=0, red_flag=False))
    r1 = client.get("/api/token/check", params={"address": ADDR}, headers={"X-Wallet": "UserAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    r2 = client.get("/api/token/check", params={"address": ADDR}, headers={"X-Wallet": "UserBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["cached"] is False and r2.json()["cached"] is True
    assert len(calls) == 1                                 # платные проверки — один раз на токен


def test_endpoint_per_session_rate_limit(client, monkeypatch):
    monkeypatch.setattr(appmod.dexadapter, "token_twitter", lambda a: "")
    monkeypatch.setattr(appmod.rugcheck, "check", lambda a: None)
    h = {"X-Wallet": "UserCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
    assert client.get("/api/token/check", params={"address": ADDR}, headers=h).status_code == 200
    other = ADDR.replace("Token", "Other")
    assert client.get("/api/token/check", params={"address": other}, headers=h).status_code == 429


def test_endpoint_rejects_bad_address(client):
    assert client.get("/api/token/check", params={"address": "nope"}).status_code == 400
