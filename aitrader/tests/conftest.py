"""Общие фикстуры: тесты не ходят в сеть к внешним детекторам (тест может переопределить)."""
import pytest

import rugcheck


@pytest.fixture(autouse=True)
def _no_rugcheck_network(monkeypatch):
    def offline(*a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(rugcheck.httpx, "get", offline)
    rugcheck._cache.clear()
