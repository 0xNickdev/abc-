"""Слоёный запуск (щит HOTBOT «layered launch»): дев или первые покупатели раздали токен по другим
кошелькам, не продавая, — заготовка под слив с «чистых» адресов.

Поток видит только сделки, переводы токенов в нём нет. Поэтому сверяем: сколько токенов у кошелька
запуска должно быть по нашим сделкам (купил − продал) и сколько на нём реально лежит (RPC
getTokenAccountsByOwner). Заметно меньше — токены ушли переводом. Один запрос на кошелёк, ответ
кэшируется; пока ответа нет, сканер ждёт недолго и идёт дальше (сеть никогда не блокирует вход).
"""
from __future__ import annotations

import os
import threading
import time

MAX_WALLETS = int(os.getenv("ABC_LAYERED_MAX_WALLETS", "6") or 6)       # дев + крупнейшие покупатели запуска
MOVED_SHARE = float(os.getenv("ABC_LAYERED_MOVED_SHARE", "0.2") or 0.2)   # ушло ≥ этой доли учтённого остатка
MIN_SUPPLY_PCT = float(os.getenv("ABC_LAYERED_MIN_PCT", "0.3") or 0.3)    # и ≥ этой доли саплая, %
MIN_WALLETS = int(os.getenv("ABC_LAYERED_MIN_WALLETS", "2") or 2)         # сколько таких кошельков = слоёный запуск
DEV_PCT = float(os.getenv("ABC_LAYERED_DEV_PCT", "1.0") or 1.0)           # или дев один увёл ≥ столько % саплая
TTL_S = float(os.getenv("ABC_LAYERED_TTL_S", "600") or 600)
WAIT_S = float(os.getenv("ABC_LAYERED_WAIT_S", "90") or 90)
RPM = int(os.getenv("ABC_LAYERED_RPM", "60") or 60)


def enabled() -> bool:
    return os.getenv("ABC_LAYERED", "1").strip().lower() not in ("0", "false", "off", "") and bool(os.getenv("SOLANA_RPC_URL"))


def verdict_of(wallets: list, actual: dict, supply: int) -> dict:
    """wallets: [(кошелёк, учтённые токены, это_дев)], actual: кошелёк → реальный остаток (нет ключа — не узнали)."""
    moved, dev_pct, seen = [], 0.0, 0
    for w, tracked, is_dev in wallets:
        if w not in actual or tracked <= 0:
            continue
        seen += 1
        gone = tracked - actual[w]
        pct = 100.0 * gone / supply if supply > 0 else 0.0
        if gone >= tracked * MOVED_SHARE and pct >= MIN_SUPPLY_PCT:
            moved.append((w, round(pct, 2)))
            if is_dev:
                dev_pct = pct
    layered = len(moved) >= MIN_WALLETS or dev_pct >= DEV_PCT
    return dict(layered=layered, moved_wallets=len(moved), moved_pct=round(sum(p for _, p in moved), 2),
                dev_moved_pct=round(dev_pct, 2), checked=seen)


class LayeredCheck:
    def __init__(self, rpc=None):
        self._rpc = rpc                         # (method, params) → result; тесты подменяют
        self.lock = threading.Lock()
        self.cache: dict[str, tuple] = {}       # mint → (ts, verdict)
        self._q: dict[str, tuple] = {}          # mint → (ts запроса, wallets, supply)
        self._calls: list[float] = []
        self._thread: threading.Thread | None = None
        self.stats = dict(state="off", checked=0, layered=0, rpc_calls=0, rpc_errors=0)

    def rpc(self, method: str, params: list):
        if self._rpc:
            return self._rpc(method, params)
        import httpx
        r = httpx.post(os.getenv("SOLANA_RPC_URL", ""), json=dict(jsonrpc="2.0", id=1, method=method, params=params), timeout=10.0)
        if r.status_code == 429:
            raise RuntimeError("429")
        j = r.json()
        if j.get("error"):
            raise RuntimeError(str(j["error"])[:120])
        return j.get("result")

    def balance(self, wallet: str, mint: str) -> int:
        self._calls.append(time.time())
        self.stats["rpc_calls"] += 1
        res = self.rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
        total = 0
        for acc in res.get("value") or []:
            try:
                total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                pass
        return total

    def request(self, mint: str, wallets: list, supply: int):
        """Поставить токен на проверку. wallets: [(кошелёк, учтённые токены, это_дев)]."""
        if not (enabled() or self._rpc) or not wallets:
            return
        now = time.time()
        with self.lock:
            hit = self.cache.get(mint)
            if (hit and now - hit[0] < TTL_S) or mint in self._q:
                return
            self._q[mint] = (now, wallets[:MAX_WALLETS], supply)
        self.start()

    def verdict(self, mint: str) -> dict | None:
        with self.lock:
            hit = self.cache.get(mint)
        return hit[1] if hit and time.time() - hit[0] < TTL_S else None

    def pending(self, mint: str) -> bool:
        """Запрос в работе и ждём его не дольше WAIT_S — сканеру есть смысл подождать следующий проход."""
        with self.lock:
            q = self._q.get(mint)
        return bool(q) and time.time() - q[0] < WAIT_S

    def tick(self) -> int:
        n = 0
        while True:
            now = time.time()
            self._calls = [t for t in self._calls if t > now - 60]
            with self.lock:
                if not self._q or len(self._calls) + MAX_WALLETS > RPM:
                    break
                mint = min(self._q, key=lambda m: self._q[m][0])
                _, wallets, supply = self._q[mint]
            actual = {}
            try:
                for w, _tracked, _dev in wallets:
                    actual[w] = self.balance(w, mint)
            except Exception as e:
                self.stats["rpc_errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:120]
                with self.lock:
                    self._q.pop(mint, None)            # не узнали — не судим, повторим при следующем запросе
                break
            v = verdict_of(wallets, actual, supply)
            with self.lock:
                self.cache[mint] = (time.time(), v)
                self._q.pop(mint, None)
                if len(self.cache) > 5000:
                    for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:1000]:
                        self.cache.pop(k, None)
            self.stats["checked"] += 1
            self.stats["layered"] += int(v["layered"])
            n += 1
        return n

    def _loop(self):
        while True:
            try:
                if not self.tick():
                    time.sleep(0.5)
            except Exception as e:                     # никогда не роняет сервер
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:120]
                time.sleep(5)

    def start(self):
        if self._thread is None and enabled():
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, name="layered", daemon=True)
            self._thread.start()


CHECK = LayeredCheck()
