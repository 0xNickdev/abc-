"""Кто профинансировал кошелёк: инсайдеры и бандлеры без Mobula (16.10).

Для держателя токена берём его самую первую транзакцию (Helius RPC) и ищем, кто прислал ему
SOL. Из этого:
  инсайдер — кошелёк, деньги на который прислал сам дев (или тот же источник, что и деву);
  бандл    — 3+ держателя с одним и тем же источником денег;
  свежий   — у кошелька меньше FRESH_SIGS транзакций за всю жизнь.
Источник, раздавший SOL на HUB_FANOUT+ кошельков из нашего кэша, — биржа/сервис, не связь.

Helius платный по запросам: кэш навсегда (источник денег кошелька не меняется, база
flow_funders.json), жёсткий бюджет запросов в минуту, приоритетная очередь (кандидаты бота
раньше датасета). Нет ответа — признаки None, а не догадки. Env: ABC_FUNDING=0 выключить,
ABC_FUNDING_RPM — запросов в минуту (20: живая проверка 16.10 — Helius отвечает 429 при
агрессивном темпе, а бот делит с этим лимит; свежих кошельков среди держателей pump-токенов
мало — снайперы на «прогретых», поэтому бандлы надёжнее ловит pumpflow по слотам запуска).
"""
from __future__ import annotations

import heapq
import itertools
import json
import os
import pathlib
import threading
import time

FRESH_SIGS = 50             # меньше подписей за жизнь — «свежий» кошелёк (и вся история видна)
HUB_FANOUT = 30             # источник, профинансировавший столько кошельков, — хаб (биржа)
BUNDLE_MIN = 3
CACHE_CAP = 300_000


def enabled() -> bool:
    return os.getenv("ABC_FUNDING", "1").strip().lower() not in ("0", "false", "off", "no")


def _rpm() -> int:
    try:
        return max(1, int(os.getenv("ABC_FUNDING_RPM", "20")))
    except ValueError:
        return 20


class FundingResolver:
    def __init__(self, rpc=None, path: pathlib.Path | None = None):
        self._rpc = rpc                                  # (method, params) → result; тесты подменяют
        self._path = path
        self.lock = threading.Lock()
        self.cache: dict[str, list] = {}                 # wallet → [funder|None, fresh(bool), first_ts]
        self._fanout: dict[str, int] = {}                # funder → сколько кошельков профинансировал
        self._q: list = []                               # (priority, seq, wallet)
        self._queued: set = set()
        self._seq = itertools.count()
        self._calls: list[float] = []
        self._thread: threading.Thread | None = None
        self._saved = time.time()
        self.stats = dict(state="off", resolved=0, rpc_calls=0, rpc_errors=0, queued=0, cached=0)

    # ── сеть ──
    def rpc(self, method: str, params: list):
        if self._rpc:
            return self._rpc(method, params)
        import httpx
        url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        r = httpx.post(url, json=dict(jsonrpc="2.0", id=1, method=method, params=params), timeout=15.0)
        if r.status_code == 429:
            raise RuntimeError("429")
        j = r.json()
        if j.get("error"):
            raise RuntimeError(str(j["error"])[:120])
        return j.get("result")

    def _budget_ok(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if t > now - 60]
        return len(self._calls) < _rpm()

    def _call(self, method: str, params: list):
        self._calls.append(time.time())
        self.stats["rpc_calls"] += 1
        return self.rpc(method, params)

    def resolve(self, wallet: str) -> list:
        """[funder|None, fresh, first_ts]. Старый кошелёк (≥FRESH_SIGS) — источник не ищем."""
        sigs = self._call("getSignaturesForAddress", [wallet, {"limit": FRESH_SIGS}]) or []
        if len(sigs) >= FRESH_SIGS or not sigs:
            return [None, False, None]
        oldest = sigs[-1]
        tx = self._call("getTransaction", [oldest["signature"],
                                           {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) or {}
        funder = _find_funder(tx, wallet)
        return [funder, True, oldest.get("blockTime")]

    # ── очередь ──
    def request(self, wallets, priority: int = 5):
        """Поставить кошельки на выяснение (меньше priority — раньше)."""
        if not enabled():
            return
        with self.lock:
            for w in wallets:
                if w and w not in self.cache and w not in self._queued:
                    heapq.heappush(self._q, (priority, next(self._seq), w))
                    self._queued.add(w)
            if len(self._q) > 50_000:                    # датасет не должен копить бесконечный хвост
                self._q = heapq.nsmallest(20_000, self._q)
                self._queued = {w for _, _, w in self._q}
            self.stats["queued"] = len(self._q)
        self.start()

    def tick(self) -> int:
        n = 0
        while self._budget_ok():
            with self.lock:
                if not self._q:
                    break
                _, _, w = heapq.heappop(self._q)
                self._queued.discard(w)
            try:
                res = self.resolve(w)
            except Exception as e:
                self.stats["rpc_errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:120]
                if "429" in str(e):
                    time.sleep(5)
                break
            with self.lock:
                self.cache[w] = res
                if res[0]:
                    self._fanout[res[0]] = self._fanout.get(res[0], 0) + 1
                if len(self.cache) > CACHE_CAP:
                    for k in list(self.cache)[:CACHE_CAP // 5]:
                        self.cache.pop(k, None)
            self.stats["resolved"] += 1
            n += 1
        self.stats["queued"] = len(self._q)
        self.stats["cached"] = len(self.cache)
        if time.time() - self._saved > 600:
            self.save()
        return n

    def _loop(self):
        while True:
            try:
                if not self.tick():
                    time.sleep(1.0)
            except Exception as e:                       # никогда не роняет сервер
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:120]
                time.sleep(5)

    def start(self):
        if self._thread is None and enabled():
            self.load()
            self.stats["state"] = "on"
            self._thread = threading.Thread(target=self._loop, name="funding", daemon=True)
            self._thread.start()

    # ── признаки ──
    def is_hub(self, funder: str | None) -> bool:
        return bool(funder) and self._fanout.get(funder, 0) >= HUB_FANOUT

    def features(self, holders: list, creator: str | None) -> dict:
        """holders: [(wallet, доля саплая %)] на момент образца. Считаем только по выясненным."""
        known = [(w, pct, self.cache[w]) for w, pct in holders if w in self.cache]
        out = dict(fund_known=len(known), fund_total=len(holders), insiders_n=None, insiders_pct=None,
                   bundle_max=None, bundle_pct=None, fresh_n=None, fresh_pct=None)
        if not known:
            return out
        dev_funder = (self.cache.get(creator) or [None])[0] if creator else None
        ins = [(w, p) for w, p, c in known
               if c[0] and not self.is_hub(c[0]) and (c[0] == creator or (dev_funder and c[0] == dev_funder))]
        groups: dict[str, list] = {}
        for w, p, c in known:
            if c[0] and not self.is_hub(c[0]):
                groups.setdefault(c[0], []).append(p)
        big = [g for g in groups.values() if len(g) >= BUNDLE_MIN]
        fresh = [(w, p) for w, p, c in known if c[1]]
        out.update(insiders_n=len(ins), insiders_pct=round(sum(p for _, p in ins), 2),
                   bundle_max=max((len(g) for g in groups.values()), default=0),
                   bundle_pct=round(sum(sum(g) for g in big), 2),
                   fresh_n=len(fresh), fresh_pct=round(sum(p for _, p in fresh), 2))
        return out

    # ── диск ──
    def _file(self) -> pathlib.Path:
        if self._path:
            return self._path
        d = os.getenv("ABC_DATA_DIR", "").strip()
        base = pathlib.Path(d) if d else pathlib.Path(__file__).resolve().parent / "outputs"
        return base / "flow_funders.json"

    def save(self):
        self._saved = time.time()
        try:
            with self.lock:
                data = json.dumps(self.cache, separators=(",", ":"))
            f = self._file()
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(data)
            tmp.replace(f)
        except OSError:
            pass

    def load(self):
        try:
            self.cache = json.loads(self._file().read_text())
        except (OSError, ValueError):
            self.cache = {}
        self._fanout = {}
        for c in self.cache.values():
            if c and c[0]:
                self._fanout[c[0]] = self._fanout.get(c[0], 0) + 1

    def snapshot(self) -> dict:
        return dict(self.stats, hubs=sum(1 for v in self._fanout.values() if v >= HUB_FANOUT), rpm=_rpm())


def _find_funder(tx: dict, wallet: str) -> str | None:
    """Первая входящая передача SOL на кошелёк в транзакции (system transfer / createAccount)."""
    if not tx:
        return None
    msg = (tx.get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs.extend(inner.get("instructions") or [])
    for ix in ixs:
        p = ix.get("parsed")
        if not isinstance(p, dict) or ix.get("program") != "system":
            continue
        info = p.get("info") or {}
        if p.get("type") in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
            return info.get("source")
        if p.get("type") in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
            return info.get("source")
    return None


FUND = FundingResolver()
