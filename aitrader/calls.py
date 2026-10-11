"""Сканер коллов: что коллеры постят в X и публичных Telegram-каналах и как эти коллы сыграли.

Источники (бета, 11.10 — «максимальная комбинация»):
  1) X-аккаунты из списка оператора — getxapi advanced_search, ОДИН запрос на пачку
     `(from:a OR from:b …)` → десятки коллеров за копейки (~$0.001/запрос);
  2) публичные Telegram-каналы — веб-превью t.me/s/<канал>: без логина, ключей и аккаунта;
  3) автопоиск — для токенов, прошедших наш скрин, ищем в X, кто постил контракт; эти авторы
     копят рейтинг, а лучшие по факту результата сами попадают в отслеживаемые.

Колл засчитывается, только если DexScreener знает контракт на Solana (отсекает мусорные
совпадения base58). Результат = цена сейчас / пик за 48 ч против цены в момент обнаружения.
Хранение — JSON в каталоге данных (переживает пересоздание контейнера).
"""
from __future__ import annotations

import html as _html
import json
import pathlib
import re
import threading
import time

import httpx

import xapi

CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_LINK_RE = re.compile(r'href="([^"]*(?:pump\.fun|dexscreener|birdeye|solscan|gmgn|photon|axiom|bullx|jup\.ag)[^"]*)"', re.I)
_TG_NAME = re.compile(r"^[A-Za-z0-9_]{4,64}$")
_X_NAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")

X_EVERY_S = 120.0          # опрос X-списка
TG_EVERY_S = 120.0         # опрос Telegram-каналов
PRICE_EVERY_S = 300.0      # обновление цен/пиков
DISCOVER_EVERY_S = 30.0    # не чаще одного автопоиска в 30 с
X_CHUNK = 10               # аккаунтов в одном поисковом запросе
TRACK_H = 48               # сколько часов следим за ценой после колла
KEEP_CALLS = 3000
WIN_X = 2.0                # «попадание» = пик ≥ 2x от цены колла
AUTO_MIN_CALLS, AUTO_MIN_HIT, AUTO_MAX = 5, 0.4, 30


def _now() -> float:
    return time.time()


def _quotes(cas: list[str]) -> dict[str, dict]:
    """Цена, капа и символ пачкой из DexScreener (только пулы, где наш токен — base)."""
    out: dict[str, dict] = {}
    for i in range(0, len(cas), 30):
        chunk = [c for c in cas[i:i + 30] if c]
        if not chunk:
            continue
        try:
            r = httpx.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=10.0)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
        except Exception:
            continue
        best: dict[str, float] = {}
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            bt = p.get("baseToken") or {}
            mint = bt.get("address")
            try:
                px = float(p.get("priceUsd") or 0)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                mc = float(p.get("marketCap") or p.get("fdv") or 0)
            except (TypeError, ValueError):
                continue
            if mint in chunk and px > 0 and liq >= best.get(mint, -1.0):
                best[mint] = liq
                out[mint] = dict(price=px, mcap=mc, symbol=str(bt.get("symbol") or "")[:20])
    return out


def extract_cas(text: str) -> list[str]:
    seen, out = set(), []
    for m in CA_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def parse_tg(page: str, channel: str) -> list[dict]:
    """Посты из веб-превью t.me/s/<канал>: id, время, текст, контракты (из текста и ссылок)."""
    posts = []
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        pm = re.search(r'data-post="([^"]+)"', block)
        tm = re.search(r'<time datetime="([^"]+)"', block)
        txm = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        raw = txm.group(1) if txm else ""
        text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text).strip()
        links = " ".join(_LINK_RE.findall(block))
        cas = extract_cas(text + " " + links)
        if not pm or not cas:
            continue
        ts = _parse_iso(tm.group(1)) if tm else _now()
        posts.append(dict(id=pm.group(1), ts=ts, text=text[:160], cas=cas,
                          url=f"https://t.me/{pm.group(1)}", author=channel))
    return posts


def _parse_iso(s: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


class CallScanner:
    def __init__(self, out_dir: pathlib.Path, log_fn=None):
        self.path = pathlib.Path(out_dir) / "calls.json"
        self.src_path = pathlib.Path(out_dir) / "calls_sources.json"
        self.log = log_fn or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.calls: list[dict] = []            # {ca, source, author, ts, url, text, price0, mcap0, discovered}
        self.tokens: dict[str, dict] = {}      # ca -> {symbol, price_now, mcap_now, peak, updated}
        self.sources = dict(x=[], tg=[])
        self.discovered_cas: set[str] = set()
        self.stats = dict(x_runs=0, tg_runs=0, price_runs=0, discover_runs=0, errors=0,
                          last_error=None, last_x=None, last_tg=None)
        self._t = dict(x=0.0, tg=0.0, price=0.0, discover=0.0)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._load()

    # ── хранение ──
    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            self.calls = d.get("calls", [])[-KEEP_CALLS:]
            self.tokens = d.get("tokens", {})
            self.discovered_cas = set(d.get("discovered", []))
        except Exception:
            pass
        try:
            s = json.loads(self.src_path.read_text())
            self.sources = dict(x=self.clean_list(s.get("x"), _X_NAME), tg=self.clean_list(s.get("tg"), _TG_NAME))
        except Exception:
            pass

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                d = dict(calls=self.calls[-KEEP_CALLS:], tokens=self.tokens,
                         discovered=sorted(self.discovered_cas)[-5000:])
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False))
            tmp.replace(self.path)
        except Exception as e:
            self._err(e)

    @staticmethod
    def clean_list(items, rx) -> list[str]:
        out = []
        for it in items or []:
            v = str(it).strip().lstrip("@")
            v = re.sub(r"^https?://(t\.me|x\.com|twitter\.com)/(s/)?", "", v).strip("/")
            if rx.match(v) and v.lower() not in {o.lower() for o in out}:
                out.append(v)
        return out[:200]

    def set_sources(self, x=None, tg=None) -> dict:
        if x is not None:
            self.sources["x"] = self.clean_list(x, _X_NAME)
        if tg is not None:
            self.sources["tg"] = self.clean_list(tg, _TG_NAME)
        try:
            self.src_path.parent.mkdir(parents=True, exist_ok=True)
            self.src_path.write_text(json.dumps(self.sources))
        except Exception as e:
            self._err(e)
        return dict(self.sources)

    def _err(self, e):
        self.stats["errors"] += 1
        self.stats["last_error"] = f"{type(e).__name__}: {e}"[:200]

    # ── запись коллов ──
    def _add(self, source: str, author: str, posts: list[dict], discovered: bool = False) -> int:
        """posts: [{ca(s), ts, url, text}] → валидируем контракты через DexScreener и пишем."""
        with self.lock:
            known = {(c["source"], c["author"].lower(), c["ca"]) for c in self.calls}
        fresh = []
        for p in posts:
            for ca in p.get("cas") or [p.get("ca")]:
                if ca and (source, (p.get("author") or author).lower(), ca) not in known:
                    fresh.append((ca, p))
        if not fresh:
            return 0
        q = _quotes(sorted({ca for ca, _ in fresh}))
        n = 0
        with self.lock:
            for ca, p in fresh:
                qt = q.get(ca)
                if not qt:
                    continue                       # не токен на Solana — мусорное совпадение
                who = p.get("author") or author
                key = (source, who.lower(), ca)
                if key in known:
                    continue
                known.add(key)
                self.calls.append(dict(ca=ca, source=source, author=who, ts=float(p.get("ts") or _now()),
                                       seen=_now(), url=p.get("url"), text=p.get("text", ""),
                                       price0=qt["price"], mcap0=qt["mcap"], discovered=discovered))
                t = self.tokens.setdefault(ca, dict(symbol=qt["symbol"], peak=qt["price"]))
                t.update(symbol=t.get("symbol") or qt["symbol"], price_now=qt["price"], mcap_now=qt["mcap"],
                         updated=_now(), peak=max(t.get("peak", 0.0), qt["price"]))
                n += 1
            self.calls = self.calls[-KEEP_CALLS:]
        return n

    # ── источники ──
    def watched_x(self) -> list[str]:
        """Список оператора + лучшие авто-найденные коллеры по факту результата."""
        base = list(self.sources["x"])
        low = {b.lower() for b in base}
        auto = [r["name"] for r in self.callers() if r["source"] == "x" and r["discovered"]
                and r["calls"] >= AUTO_MIN_CALLS and r["hit_rate"] >= AUTO_MIN_HIT and r["name"].lower() not in low]
        return base + auto[:AUTO_MAX]

    def poll_x(self) -> int:
        api_key = xapi.key()
        handles = self.watched_x()
        if not api_key or not handles:
            return 0
        n = 0
        for i in range(0, len(handles), X_CHUNK):
            q = "(" + " OR ".join(f"from:{h}" for h in handles[i:i + X_CHUNK]) + ")"
            try:
                j = xapi._get("/twitter/tweet/advanced_search", {"q": q, "product": "Latest"}, api_key)
            except Exception as e:
                self._err(e)
                continue
            if j.get("_err"):
                self._err(RuntimeError(f"getxapi {j['_err']}"))
                continue
            posts = []
            for tw in xapi._tweet_rows(j):
                if not isinstance(tw, dict):
                    continue
                u = tw.get("author") or tw.get("user") or {}
                name = str(u.get("userName") or u.get("username") or u.get("screen_name") or "").lstrip("@")
                text = str(tw.get("text") or tw.get("full_text") or "")
                cas = extract_cas(text + " " + " ".join(str(x.get("expanded_url") or "") for x in
                                                        ((tw.get("entities") or {}).get("urls") or []) if isinstance(x, dict)))
                if not name or not cas:
                    continue
                tid = str(tw.get("id") or tw.get("id_str") or "")
                posts.append(dict(author=name, cas=cas, text=re.sub(r"\s+", " ", text)[:160],
                                  ts=xapi._parse_ts(tw.get("createdAt") or tw.get("created_at")) or _now(),
                                  url=f"https://x.com/{name}/status/{tid}" if tid else f"https://x.com/{name}"))
            n += self._add("x", "", posts)
        self.stats["x_runs"] += 1
        self.stats["last_x"] = _now()
        return n

    def poll_tg(self) -> int:
        n = 0
        for ch in self.sources["tg"]:
            try:
                r = httpx.get(f"https://t.me/s/{ch}", timeout=12.0, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; abc-calls/1.0)"})
                if r.status_code != 200:
                    continue
                n += self._add("tg", ch, parse_tg(r.text, ch))
            except Exception as e:
                self._err(e)
        self.stats["tg_runs"] += 1
        self.stats["last_tg"] = _now()
        return n

    def discover(self, ca: str) -> int:
        """Кто в X постил контракт токена, прошедшего наш скрин (раз на токен, с лимитом)."""
        if not ca or ca in self.discovered_cas or not xapi.key():
            return 0
        if _now() - self._t["discover"] < DISCOVER_EVERY_S:
            return 0
        self._t["discover"] = _now()
        self.discovered_cas.add(ca)
        try:
            m = xapi.ca_mentions(ca)
        except Exception as e:
            self._err(e)
            return 0
        authors = (m or {}).get("authors") or []
        posts = [dict(author=a["username"], cas=[ca], ts=_now(), text="",
                      url=f"https://x.com/{a['username']}") for a in authors if a.get("username")]
        self.stats["discover_runs"] += 1
        return self._add("x", "", posts, discovered=True)

    def update_prices(self) -> int:
        cutoff = _now() - TRACK_H * 3600
        with self.lock:
            cas = sorted({c["ca"] for c in self.calls if c["seen"] >= cutoff})
        q = _quotes(cas)
        with self.lock:
            for ca, qt in q.items():
                t = self.tokens.setdefault(ca, dict(symbol=qt["symbol"], peak=qt["price"]))
                t.update(price_now=qt["price"], mcap_now=qt["mcap"], updated=_now(),
                         peak=max(t.get("peak", 0.0), qt["price"]))
        self.stats["price_runs"] += 1
        return len(q)

    # ── выдача ──
    def callers(self) -> list[dict]:
        with self.lock:
            calls, tokens = list(self.calls), dict(self.tokens)
        by: dict[tuple, dict] = {}
        for c in calls:
            t = tokens.get(c["ca"]) or {}
            p0 = c.get("price0") or 0
            mult = (t.get("peak", 0) / p0) if p0 else 0.0
            st = by.setdefault((c["source"], c["author"].lower()), dict(
                name=c["author"], source=c["source"], calls=0, hits=0, mults=[], last=0.0, discovered=True))
            st["calls"] += 1
            st["hits"] += mult >= WIN_X
            st["mults"].append(mult)
            st["last"] = max(st["last"], c["ts"])
            st["discovered"] = st["discovered"] and bool(c.get("discovered"))
        out = []
        for st in by.values():
            m = sorted(st.pop("mults"))
            out.append(dict(st, hit_rate=round(st["hits"] / st["calls"], 2) if st["calls"] else 0.0,
                            median_x=round(m[len(m) // 2], 2) if m else 0.0))
        out.sort(key=lambda r: (-(r["calls"] >= 3), -r["hit_rate"], -r["median_x"], -r["calls"]))
        return out

    def feed(self, source: str = "all", hours: float = 24.0, limit: int = 60) -> list[dict]:
        cutoff = _now() - hours * 3600
        rating = {(r["source"], r["name"].lower()): r for r in self.callers()}
        with self.lock:
            calls = [c for c in self.calls if c["seen"] >= cutoff and (source == "all" or c["source"] == source)]
            tokens = dict(self.tokens)
        by: dict[str, dict] = {}
        for c in sorted(calls, key=lambda c: c["ts"]):
            t = tokens.get(c["ca"]) or {}
            row = by.setdefault(c["ca"], dict(ca=c["ca"], symbol=t.get("symbol") or c["ca"][:6],
                                              first_ts=c["ts"], price0=c["price0"], mcap0=c["mcap0"],
                                              callers=[]))
            r = rating.get((c["source"], c["author"].lower())) or {}
            row["callers"].append(dict(name=c["author"], source=c["source"], url=c.get("url"), ts=c["ts"],
                                       calls=r.get("calls", 1), hit_rate=r.get("hit_rate", 0.0),
                                       median_x=r.get("median_x", 0.0), text=c.get("text", "")))
            row["last_ts"] = c["ts"]
        out = []
        for row in by.values():
            t = tokens.get(row["ca"]) or {}
            p0 = row["price0"] or 0
            row.update(mcap_now=t.get("mcap_now"), x_now=round(t.get("price_now", 0) / p0, 2) if p0 else None,
                       peak_x=round(t.get("peak", 0) / p0, 2) if p0 else None)
            out.append(row)
        out.sort(key=lambda r: -r["last_ts"])
        return out[:limit]

    # ── цикл ──
    def tick(self):
        now = _now()
        changed = 0
        if now - self._t["x"] >= X_EVERY_S:
            self._t["x"] = now
            changed += self.poll_x()
        if now - self._t["tg"] >= TG_EVERY_S:
            self._t["tg"] = now
            changed += self.poll_tg()
        if now - self._t["price"] >= PRICE_EVERY_S:
            self._t["price"] = now
            changed += self.update_prices()
        if changed:
            self._save()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        def loop():
            while not self._stop.wait(15.0):
                try:
                    self.tick()
                except Exception as e:      # сканер коллов никогда не роняет сервер
                    self._err(e)
        self._thread = threading.Thread(target=loop, name="calls", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
