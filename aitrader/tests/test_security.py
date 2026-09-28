"""Аудит безопасности 28.09: публичный URL не должен давать случайному посетителю
управлять house-ботом, выводить session-кошелёк, стартовать чужой бот по публичному pubkey,
жечь недельный котёл house-бота фейковыми сделками."""
import base64 as b64

import base58
import pytest
from nacl.signing import SigningKey
from test_app import _mu_client, _wallet_auth

import app as appmod

pytestmark = pytest.mark.realauth


@pytest.fixture
def pub(tmp_path, monkeypatch):
    """Клиент в режиме публичного деплоя с admin-кошельком."""
    c = _mu_client(tmp_path, monkeypatch)
    monkeypatch.setattr(appmod, "PUBLIC_DEPLOY", True)
    monkeypatch.setattr(appmod, "ADMIN_MODE", False)
    return c


class TestHouseSession:
    def test_stranger_cannot_touch_house_bot(self, pub):
        for path, body in [("/api/bot/stop", None), ("/api/bot/config", {"poll_s": 0}),
                           ("/api/filters", {"min_liquidity": 1}), ("/api/mode", {"mode": "LIVE"}),
                           ("/api/settings", {"trending_cmd": "gmgn-cli market trending"})]:
            r = pub.post(path, json=body or {})
            assert r.status_code == 403, path

    def test_admin_wallet_token_unlocks_house(self, pub, monkeypatch):
        pk, hdr = _wallet_auth(pub)
        monkeypatch.setattr(appmod, "ADMIN_WALLETS", frozenset({pk}))
        r = pub.post("/api/bot/config", json={"poll_s": 0}, headers={"X-Auth": hdr["X-Auth"]})
        assert r.status_code == 200
        assert r.json()["cfg"]["poll_s"] == 5.0          # busy-loop зажат

    def test_house_session_wallet_withdraw_blocked(self, pub, monkeypatch):
        monkeypatch.setattr(appmod.sessionwallet, "withdraw_all", lambda kp, to: "SIG")
        r = pub.post("/api/session-wallet/withdraw", json={"to": "Attacker1111111111111111111111111111"})
        assert r.status_code == 403

    def test_local_dev_still_open(self, tmp_path, monkeypatch):
        c = _mu_client(tmp_path, monkeypatch)
        monkeypatch.setattr(appmod, "PUBLIC_DEPLOY", False)
        assert c.post("/api/bot/config", json={"poll_s": 30}).status_code == 200


class TestWalletSession:
    def test_pubkey_alone_is_not_enough(self, pub):
        pk = base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()
        r = pub.post("/api/bot/start", json={"chain": "sol"}, headers={"X-Wallet": pk})
        assert r.status_code == 401
        r = pub.post("/api/bot/config", json={"mode": "n3"}, headers={"X-Wallet": pk})
        assert r.status_code == 401

    def test_signed_in_owner_can_write(self, pub):
        pk, hdr = _wallet_auth(pub)
        assert pub.post("/api/filters", json={"min_liquidity": 9000}, headers=hdr).status_code == 200

    def test_withdraw_only_to_self(self, pub, monkeypatch):
        monkeypatch.setattr(appmod.sessionwallet, "withdraw_all", lambda kp, to: "SIG")
        pk, hdr = _wallet_auth(pub)
        bad = pub.post("/api/session-wallet/withdraw",
                       json={"to": "Attacker1111111111111111111111111111"}, headers=hdr)
        assert bad.status_code == 403
        ok = pub.post("/api/session-wallet/withdraw", json={}, headers=hdr)
        assert ok.status_code == 200 and ok.json()["to"] == pk


class TestAuthChallenge:
    def test_second_challenge_does_not_break_first(self, pub):
        sk = SigningKey.generate()
        pk = base58.b58encode(bytes(sk.verify_key)).decode()
        m1 = pub.post("/api/auth/challenge", json={"pubkey": pk}).json()["message"]
        pub.post("/api/auth/challenge", json={"pubkey": pk})          # чужой запрос на тот же pk
        sig = b64.b64encode(sk.sign(m1.encode()).signature).decode()
        r = pub.post("/api/auth/verify", json={"pubkey": pk, "signature": sig})
        assert r.status_code == 200 and r.json()["token"]
        r2 = pub.post("/api/auth/verify", json={"pubkey": pk, "signature": sig})
        assert r2.status_code == 401                                 # nonce одноразовый


class TestAbuse:
    def test_week_budget_ignores_wallet_sessions(self, pub):
        pk, hdr = _wallet_auth(pub)
        s = appmod.get_session(pk)
        before = appmod.WEEK_BUDGET.current()
        s.positions = [dict(symbol="F", address="FAKE", size_sol=0.5, pnl=-0.9, cycles=0,
                            entry={}, chain="sol", entry_price=1.0)]
        appmod.do_sell("FAKE", 1.0, "x", s)
        assert appmod.WEEK_BUDGET.current() == before

    def test_farm_endpoint_throttled(self, pub, monkeypatch):
        monkeypatch.setattr(appmod.MK, "adapter_for", lambda ch: object())
        assert pub.get("/api/token/farm?address=X").status_code == 200
        assert pub.get("/api/token/farm?address=X").status_code == 429

    def test_junk_wallet_header_rejected(self, pub):
        assert pub.get("/api/bot", headers={"X-Wallet": "../../etc"}).status_code == 400

    def test_redact_hides_api_key(self):
        msg = "Client error for url https://mainnet.helius-rpc.com/?api-key=abc123-SECRET&x=1"
        assert "abc123" not in appmod._redact(msg) and "api-key=***" in appmod._redact(msg)
