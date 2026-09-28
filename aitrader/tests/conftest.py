"""Общие фикстуры: тесты не ходят в сеть к внешним детекторам (тест может переопределить)."""
import pytest

import rugcheck


@pytest.fixture(autouse=True)
def _no_rugcheck_network(monkeypatch):
    def offline(*a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(rugcheck.httpx, "get", offline)
    rugcheck._cache.clear()
    import app as appmod
    appmod._THROTTLE.clear()


def pytest_configure(config):
    config.addinivalue_line("markers", "realauth: не обходить _guard_write (тесты авторизации)")


@pytest.fixture(autouse=True)
def _auth_bypass(request, monkeypatch):
    """Функциональные тесты не про авторизацию: кошельковые сессии считаем вошедшими.
    Тесты с @pytest.mark.realauth проверяют настоящий гейт."""
    if request.node.get_closest_marker("realauth"):
        return
    import app as appmod
    monkeypatch.setattr(appmod, "_guard_write", lambda sess, x_auth: appmod._block_if_public())
