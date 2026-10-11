"""Argus — Telegram-бот аналитики коллов (как SpyDefi, но на своих ончейн-данных).

Чем сильнее SpyDefi:
  • пики коллов в реальном времени: цена pump-токенов из своего потока (pumpflow, мс после
    сделки), а не опрос раз в минуты → «Achievement xN» прилетает, пока токен ещё летит;
  • контекст в момент колла: перевес покупок, уникальные покупатели, снайперы блока создания,
    продажи дева, наши smart-кошельки в токене — видно, коллил ли человек живой токен или
    сливал в него аудиторию;
  • Signal: один токен независимо коллят несколько коллеров с подтверждённым хитрейтом;
  • прислал контракт — получил отчёт: поток, кто коллил, на какой капе, сколько сейчас.

Транспорт — long polling getUpdates (без вебхука/домена). Работает, только если задан
ARGUS_BOT_TOKEN; ARGUS_CHANNEL (@канал или id, бот — админ) — куда публиковать ачивки
и сигналы, помимо подписавшихся чатов (/alerts on). Состояние — argus_state.json.
Ошибки Telegram/сети никогда не роняют сервер.
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import threading
import time

import httpx

LEVELS = (2, 3, 5, 10, 20, 50, 100)
ENGINE_EVERY_S = 5.0
TRACK_S = 48 * 3600
SIGNAL_WINDOW_S = 3600        # колл от разных коллеров в течение часа от первого
SIGNAL_MIN_CALLERS = 3
SIGNAL_MIN_HIT = 0.35         # хитрейт коллера (при ≥3 коллах), чтобы его голос считался
SEND_GAP_S = 0.06             # глобальный темп (лимит Telegram ~30 msg/s)
CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")


def token() -> str:
    return os.getenv("ARGUS_BOT_TOKEN", "").strip()


def channel() -> str:
    return os.getenv("ARGUS_CHANNEL", "").strip()


def enabled() -> bool:
    return bool(token()) and os.getenv("ABC_ARGUS", "1").strip().lower() not in ("0", "false", "off", "no")


# ── формат ───────────────────────────────────────────────────────────────────
def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def usd(v) -> str:
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.1f}{suf}".replace(".0" + suf, suf)
    return f"${v:.0f}"


def dur(s: float) -> str:
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def xfmt(m) -> str:
    try:
        m = float(m or 0)
    except (TypeError, ValueError):
        return "—"
    return f"x{m:.1f}" if m < 10 else f"x{m:.0f}"


def who(c: dict) -> str:
    n = esc(c.get("author") or c.get("name"))
    return f"@{n}" if c.get("source") == "x" else n


def caller_link(bot_user: str, c: dict) -> str:
    if not bot_user:
        return ""
    return f"https://t.me/{bot_user}?start=c_{c.get('source')}_{c.get('author') or c.get('name')}"


def flow_line(m: dict | None) -> str:
    """Ончейн-контекст из своего потока одной строкой."""
    if not m or not m.get("buys") and not m.get("sells"):
        return ""
    parts = []
    if m.get("buy_ratio") is not None:
        parts.append(f"buys {m['buy_ratio']:.0%}")
    parts.append(f"{m.get('unique_buyers', 0)} buyers")
    parts.append(f"net {m.get('net_flow_sol', 0):+.1f} SOL")
    if m.get("snipers"):
        parts.append(f"{m['snipers']} snipers")
    if m.get("dev_sell_sol"):
        parts.append(f"dev sold {m['dev_sell_sol']:.1f} SOL")
    if m.get("tracked_in"):
        parts.append(f"{m['tracked_in']} smart wallets in")
    return " · ".join(parts)


class Argus:
    def __init__(self, scanner, out_dir: pathlib.Path, flow=None, sol_usd=None, log_fn=None):
        self.calls = scanner
        self.flow = flow
        self.sol_usd = sol_usd or (lambda: 0.0)
        self.log = log_fn or (lambda *a, **k: None)
        self.path = pathlib.Path(out_dir) / "argus_state.json"
        self.lock = threading.Lock()
        self.st = dict(offset=0, subs=[], sent={}, signals={}, snap={}, primed=False)
        self.bot_user = ""
        self.stats = dict(state="off", updates=0, sent=0, send_errors=0, achievements=0, signals=0,
                          live_peaks=0, last_error=None)
        self._last_send = 0.0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._load()

    # ── хранение ──
    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            for k in self.st:
                if k in d:
                    self.st[k] = d[k]
        except Exception:
            pass

    def _save(self):
        try:
            with self.lock:
                d = json.dumps(self.st, ensure_ascii=False)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(d)
            tmp.replace(self.path)
        except Exception as e:
            self._err(e)

    def _err(self, e):
        self.stats["last_error"] = f"{type(e).__name__}: {e}"[:200]

    # ── Telegram ──
    def api(self, method: str, http_timeout: float = 15.0, **params) -> dict:
        r = httpx.post(f"https://api.telegram.org/bot{token()}/{method}", json=params, timeout=http_timeout)
        return r.json()

    def send(self, chat, text: str, preview: bool = False) -> bool:
        gap = SEND_GAP_S - (time.time() - self._last_send)
        if gap > 0:
            time.sleep(gap)
        self._last_send = time.time()
        try:
            j = self.api("sendMessage", chat_id=chat, text=text[:4000], parse_mode="HTML",
                         link_preview_options=dict(is_disabled=not preview))
        except Exception as e:
            self.stats["send_errors"] += 1
            self._err(e)
            return False
        if not j.get("ok"):
            self.stats["send_errors"] += 1
            self._err(RuntimeError(str(j.get("description"))[:160]))
            if j.get("error_code") == 403:                  # бота заблокировали/выкинули — отписать
                with self.lock:
                    if chat in self.st["subs"]:
                        self.st["subs"].remove(chat)
            return False
        self.stats["sent"] += 1
        return True

    def send_animation(self, chat, anim, caption: str):
        """anim: bytes GIF (первая отправка) или file_id (повтор). → file_id или None."""
        gap = SEND_GAP_S - (time.time() - self._last_send)
        if gap > 0:
            time.sleep(gap)
        self._last_send = time.time()
        data = dict(chat_id=str(chat), caption=caption[:1024], parse_mode="HTML")
        try:
            url = f"https://api.telegram.org/bot{token()}/sendAnimation"
            if isinstance(anim, bytes):
                r = httpx.post(url, data=data, files=dict(animation=("argus.gif", anim, "image/gif")), timeout=60.0)
            else:
                r = httpx.post(url, data=dict(data, animation=anim), timeout=30.0)
            j = r.json()
        except Exception as e:
            self.stats["send_errors"] += 1
            self._err(e)
            return None
        if not j.get("ok"):
            self.stats["send_errors"] += 1
            self._err(RuntimeError(str(j.get("description"))[:160]))
            return None
        self.stats["sent"] += 1
        res = j.get("result") or {}
        return ((res.get("animation") or res.get("document") or {}).get("file_id")) or True

    def broadcast(self, item):
        """item: текст или {text, card?}. Карточка рендерится один раз, дальше — file_id."""
        if isinstance(item, str):
            item = dict(text=item)
        targets = list(dict.fromkeys(([channel()] if channel() else []) + list(self.st["subs"])))
        anim = None
        if item.get("card") and targets and len(item["text"]) <= 1024:
            try:
                import cards
                anim = cards.achievement_gif(**item["card"])
                self.stats["cards"] = self.stats.get("cards", 0) + 1
            except Exception as e:
                self._err(e)
        for t in targets:
            if anim is not None:
                fid = self.send_animation(t, anim, item["text"])
                if fid:
                    if isinstance(fid, str):
                        anim = fid
                    continue
            self.send(t, item["text"])                     # без карточки / карточка не ушла

    # ── живые цены и пики ──
    def _active(self) -> list[dict]:
        cut = time.time() - TRACK_S
        with self.calls.lock:
            return [dict(c) for c in self.calls.calls if c.get("seen", 0) >= cut and c.get("price0")]

    def live_peaks(self, active: list[dict]) -> int:
        """Пики из своего потока: pump-токены обновляются на каждой сделке, а не раз в 5 мин."""
        if not self.flow:
            return 0
        rate = self.sol_usd()
        if rate <= 0:
            return 0
        n = 0
        for ca in {c["ca"] for c in active}:
            m = self.flow.metrics(ca, 60)
            if not m or not m.get("price_sol") or m.get("last_trade_ago_s", 1e9) > 60:
                continue
            px = m["price_sol"] * rate
            with self.calls.lock:
                t = self.calls.tokens.get(ca)
                if not t or not t.get("price_now"):
                    continue
                # разные источники (DexScreener на колле / поток сейчас) — только если масштаб
                # согласован: иначе не-SOL пул или чужой пул нарисуют фальшивый икс
                ref = float(t["price_now"])
                if not (ref / 20 <= px <= ref * 20):
                    continue
                t["price_now"] = px
                t["live_ts"] = time.time()
                if px > t.get("peak", 0):
                    t["peak"] = px
                    t["peak_ts"] = time.time()
                    n += 1
        self.stats["live_peaks"] += n
        return n

    def snapshot_new_calls(self, active: list[dict]):
        """Ончейн-контекст на момент первого колла токена (для ачивок и отчётов)."""
        if not self.flow:
            return
        for c in active:
            ca = c["ca"]
            if ca in self.st["snap"]:
                continue
            m = self.flow.metrics(ca, 300)
            with self.lock:
                self.st["snap"][ca] = dict(ts=time.time(), line=flow_line(m)) if m else dict(ts=time.time(), line="")
        if len(self.st["snap"]) > 6000:
            with self.lock:
                for k in sorted(self.st["snap"], key=lambda k: self.st["snap"][k]["ts"])[:2000]:
                    self.st["snap"].pop(k, None)

    # ── ачивки ──
    def achievements(self, active: list[dict]) -> list[str]:
        rating = {(r["source"], r["name"].lower()): r for r in self.calls.callers()}
        with self.calls.lock:
            tokens = {k: dict(v) for k, v in self.calls.tokens.items()}
        msgs = []
        for c in active:
            t = tokens.get(c["ca"]) or {}
            mult = (t.get("peak", 0) / c["price0"]) if c["price0"] else 0
            lvl = max([lv for lv in LEVELS if mult >= lv], default=0)
            key = f"{c['source']}|{c['author'].lower()}|{c['ca']}"
            if lvl <= self.st["sent"].get(key, 0):
                continue
            with self.lock:
                self.st["sent"][key] = lvl
            if not self.st["primed"]:
                continue                                    # первый проход: старое не постим
            r = rating.get((c["source"], c["author"].lower())) or {}
            mc0 = c.get("mcap0") or 0
            card = dict(level=lvl, mult=mult, symbol=str(t.get("symbol") or c["ca"][:6]),
                        caller=("@" if c["source"] == "x" else "") + c["author"], mc0=mc0,
                        mc_peak=mc0 * mult, took=dur((t.get("peak_ts") or time.time()) - c["ts"]), seed=c["ca"])
            msgs.append(dict(text=self.fmt_achievement(c, t, lvl, mult, r), card=card))
            self.stats["achievements"] += 1
        return msgs

    def fmt_achievement(self, c, t, lvl, mult, r) -> str:
        sym = esc(t.get("symbol") or c["ca"][:6])
        mc0 = c.get("mcap0") or 0
        peak_mc = mc0 * mult if mc0 else 0
        took = (t.get("peak_ts") or time.time()) - c["ts"]
        lines = [f"<b>ACHIEVEMENT UNLOCKED · x{lvl}</b>", "",
                 f"{who(c)} called <a href=\"https://dexscreener.com/solana/{c['ca']}\">${sym}</a>",
                 f"<b>{usd(mc0)} → {usd(peak_mc)}</b>  ({xfmt(mult)} in {dur(took)})"]
        if r.get("calls", 0) >= 3:
            lines.append(f"Track record: {r['calls']} calls · {r['hit_rate']:.0%} hit x2+ · median {xfmt(r['median_x'])}")
        snap = (self.st["snap"].get(c["ca"]) or {}).get("line")
        if snap:
            lines.append(f"On-chain at call: {esc(snap)}")
        links = []
        if c.get("url"):
            links.append(f"<a href=\"{esc(c['url'])}\">View call</a>")
        cl = caller_link(self.bot_user, c)
        if cl:
            links.append(f"<a href=\"{esc(cl)}\">Caller stats</a>")
        links.append(f"<a href=\"https://dexscreener.com/solana/{c['ca']}\">Chart</a>")
        lines += ["", " · ".join(links), f"<code>{c['ca']}</code>"]
        return "\n".join(lines)

    # ── сигналы ──
    def signals(self, active: list[dict]) -> list[str]:
        rating = {(r["source"], r["name"].lower()): r for r in self.calls.callers()}
        by: dict[str, list] = {}
        for c in sorted(active, key=lambda c: c["ts"]):
            by.setdefault(c["ca"], []).append(c)
        msgs = []
        for ca, cs in by.items():
            if ca in self.st["signals"]:
                continue
            first = cs[0]["ts"]
            good, names = [], set()
            for c in cs:
                if c["ts"] - first > SIGNAL_WINDOW_S:
                    break
                r = rating.get((c["source"], c["author"].lower())) or {}
                if c["author"].lower() in names:
                    continue
                if r.get("calls", 0) >= 3 and r.get("hit_rate", 0) >= SIGNAL_MIN_HIT:
                    names.add(c["author"].lower())
                    good.append((c, r))
            if len(good) < SIGNAL_MIN_CALLERS:
                continue
            with self.lock:
                self.st["signals"][ca] = time.time()
            if not self.st["primed"] or time.time() - good[-1][0]["ts"] > 1800:
                continue                                    # старое совпадение — не сигнал
            msgs.append(dict(text=self.fmt_signal(ca, good)))
            self.stats["signals"] += 1
        return msgs

    def fmt_signal(self, ca, good) -> str:
        with self.calls.lock:
            t = dict(self.calls.tokens.get(ca) or {})
        sym = esc(t.get("symbol") or ca[:6])
        lines = [f"<b>ARGUS SIGNAL · {len(good)} proven callers on ${sym}</b>", "",
                 f"Mcap now {usd(t.get('mcap_now'))} · first call {dur(time.time() - good[0][0]['ts'])} ago at {usd(good[0][0].get('mcap0'))}"]
        for c, r in good[:6]:
            lines.append(f"• {who(c)} — {r['calls']} calls, {r['hit_rate']:.0%} hit, median {xfmt(r['median_x'])}")
        m = self.flow.metrics(ca, 300) if self.flow else None
        fl = flow_line(m)
        if fl:
            lines.append(f"Last 5m on-chain: {esc(fl)}")
        lines += ["", f"<a href=\"https://dexscreener.com/solana/{ca}\">Chart</a>", f"<code>{ca}</code>",
                  "<i>Not financial advice. Most memecoins go to zero.</i>"]
        return "\n".join(lines)

    # ── команды ──
    def handle(self, msg: dict):
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/start" and arg.startswith("c_"):
            _, src, name = arg.split("_", 2) if arg.count("_") >= 2 else ("", "", "")
            return self.send(chat, self.caller_report(name, src))
        if cmd in ("/start", "/help"):
            return self.send(chat, self.help_text())
        if cmd == "/top":
            return self.send(chat, self.top_text(arg))
        if cmd == "/hot":
            return self.send(chat, self.hot_text(arg))
        if cmd == "/card":
            return self.demo_card(chat)
        if cmd == "/caller":
            return self.send(chat, self.caller_report(arg.lstrip("@")))
        if cmd == "/alerts":
            on = arg.lower() not in ("off", "0", "stop")
            with self.lock:
                subs = self.st["subs"]
                if on and chat not in subs:
                    subs.append(chat)
                if not on and chat in subs:
                    subs.remove(chat)
            self._save()
            return self.send(chat, "Alerts <b>on</b>: achievements x2+ and Argus signals will land here." if on
                             else "Alerts <b>off</b>.")
        m = CA_RE.search(text)
        if m:
            return self.send(chat, self.token_report(m.group(0)))
        if msg.get("chat", {}).get("type") == "private":
            self.send(chat, self.help_text())

    def demo_card(self, chat):
        """Карточка по самому сильному реальному коллу за 24ч — превью того, что уходит в канал."""
        rows = [r for r in self.calls.feed("all", 24, 100) if (r.get("peak_x") or 0) >= 1.5]
        if not rows:
            return self.send(chat, "No call has reached x1.5 in the last 24h yet.")
        r = max(rows, key=lambda r: r["peak_x"])
        c0 = r["callers"][0]
        c = dict(ca=r["ca"], source=c0["source"], author=c0["name"], ts=c0["ts"], url=c0.get("url"),
                 price0=r["price0"], mcap0=r["mcap0"])
        with self.calls.lock:
            t = dict(self.calls.tokens.get(r["ca"]) or {})
        rating = {(x["source"], x["name"].lower()): x for x in self.calls.callers()}
        mult = r["peak_x"]
        lvl = max([lv for lv in LEVELS if mult >= lv], default=1)
        mc0 = r["mcap0"] or 0
        card = dict(level=max(lvl, 2), mult=mult, symbol=str(r["symbol"]), caller=("@" if c["source"] == "x" else "") + c["author"],
                    mc0=mc0, mc_peak=mc0 * mult, took=dur((t.get("peak_ts") or time.time()) - c["ts"]), seed=r["ca"])
        text = self.fmt_achievement(c, t, lvl, mult, rating.get((c["source"], c["author"].lower())) or {})
        try:
            import cards
            gif = cards.achievement_gif(**card)
        except Exception as e:
            self._err(e)
            return self.send(chat, text)
        return self.send_animation(chat, gif, text) or self.send(chat, text)

    def help_text(self) -> str:
        return ("<b>Argus</b> — sees every call and every trade.\n\n"
                "Paste a token contract — on-chain flow, who called it, at what mcap, and how it went.\n\n"
                "/top — best callers by real results (24h, 7d)\n"
                "/hot — biggest calls of the last hours\n"
                "/caller name — a caller's track record\n"
                "/card — achievement card of the strongest call today\n"
                "/alerts on|off — achievements and multi-caller signals in this chat")

    def top_text(self, arg: str = "") -> str:
        days = 7 if "7" in arg else 1
        cut = time.time() - days * 86400
        rows = [r for r in self.calls.callers() if r["last"] >= cut and r["calls"] >= 3]
        rows.sort(key=lambda r: (-r["hit_rate"], -r["median_x"], -r["calls"]))
        if not rows:
            return f"Not enough calls in the last {days * 24}h yet — callers need 3+ tracked calls."
        out = [f"<b>Top callers · {'7d' if days == 7 else '24h'}</b>  (hit = peak ≥ x2 from call)", ""]
        for i, r in enumerate(rows[:15], 1):
            c = dict(source=r["source"], author=r["name"])
            out.append(f"{i}. <a href=\"{esc(caller_link(self.bot_user, c))}\">{who(c)}</a> — "
                       f"{r['hit_rate']:.0%} hit · median {xfmt(r['median_x'])} · {r['calls']} calls")
        return "\n".join(out)

    def hot_text(self, arg: str = "") -> str:
        hours = 6
        if arg.rstrip("h").isdigit():
            hours = max(1, min(48, int(arg.rstrip("h"))))
        rows = [r for r in self.calls.feed("all", hours, 100) if r.get("peak_x")]
        rows.sort(key=lambda r: -(r["peak_x"] or 0))
        if not rows:
            return f"No calls tracked in the last {hours}h."
        out = [f"<b>Hottest calls · {hours}h</b>", ""]
        for r in rows[:12]:
            c0 = r["callers"][0]
            out.append(f"<b>{xfmt(r['peak_x'])}</b> <a href=\"https://dexscreener.com/solana/{r['ca']}\">${esc(r['symbol'])}</a>"
                       f" — {usd(r['mcap0'])} → {usd((r['mcap0'] or 0) * (r['peak_x'] or 0))}"
                       f" · first {who(c0)}" + (f" +{len(r['callers']) - 1}" if len(r["callers"]) > 1 else ""))
        return "\n".join(out)

    def caller_report(self, name: str, source: str = "") -> str:
        if not name:
            return "Usage: /caller name"
        rows = [r for r in self.calls.callers() if r["name"].lower() == name.lower()
                and (not source or r["source"] == source)]
        if not rows:
            return f"No tracked calls from <b>{esc(name)}</b> yet."
        r = rows[0]
        with self.calls.lock:
            mine = [dict(c) for c in self.calls.calls if c["author"].lower() == name.lower() and c["source"] == r["source"]]
            tokens = {c["ca"]: dict(self.calls.tokens.get(c["ca"]) or {}) for c in mine}
        c = dict(source=r["source"], author=r["name"])
        out = [f"<b>{who(c)}</b> · {'X' if r['source'] == 'x' else 'Telegram'}", "",
               f"{r['calls']} calls · {r['hit_rate']:.0%} hit x2+ · median {xfmt(r['median_x'])}", ""]
        for cl in sorted(mine, key=lambda c: -c["ts"])[:8]:
            t = tokens.get(cl["ca"]) or {}
            p0 = cl.get("price0") or 0
            peak = t.get("peak", 0) / p0 if p0 else 0
            now = t.get("price_now", 0) / p0 if p0 else 0
            out.append(f"${esc(t.get('symbol') or cl['ca'][:6])} at {usd(cl.get('mcap0'))} — peak {xfmt(peak)}, now {xfmt(now)}"
                       f" · {dur(time.time() - cl['ts'])} ago")
        return "\n".join(out)

    def token_report(self, ca: str) -> str:
        m = self.flow.metrics(ca, 300) if self.flow else None
        h = self.flow.metrics(ca, 3600) if self.flow else None
        with self.calls.lock:
            cs = sorted([dict(c) for c in self.calls.calls if c["ca"] == ca], key=lambda c: c["ts"])
            t = dict(self.calls.tokens.get(ca) or {})
        sym = esc(t.get("symbol") or (m or {}).get("symbol") or ca[:6])
        out = [f"<b>${sym}</b>  <code>{ca}</code>", ""]
        rate = self.sol_usd()
        if m and m.get("price_sol") and rate > 0:
            out.append(f"Mcap {usd(m['mcap_sol'] * rate)} · {'bonding curve ' + str(m['curve_sol']) + ' SOL' if m['venue'] == 'curve' else 'PumpSwap'}"
                       + (f" · age {dur(m['age_s'])}" if m.get("age_s") else ""))
        elif t.get("mcap_now"):
            out.append(f"Mcap {usd(t['mcap_now'])}")
        for label, mm in (("5m", m), ("1h", h)):
            fl = flow_line(mm)
            if fl:
                out.append(f"{label}: {esc(fl)}")
        if m and m.get("dev_sells"):
            out.append(f"<b>Dev sold {m['dev_sell_sol']:.2f} SOL</b> ({m['dev_sells']} sells)")
        if not (m or h) and not cs:
            out.append("No trades seen in our stream for the last hour and no tracked calls.")
        if cs:
            rating = {(r["source"], r["name"].lower()): r for r in self.calls.callers()}
            out += ["", f"<b>Called by {len(cs)}</b>"]
            for c in cs[:8]:
                p0 = c.get("price0") or 0
                peak = t.get("peak", 0) / p0 if p0 else 0
                r = rating.get((c["source"], c["author"].lower())) or {}
                rec = f" ({r['hit_rate']:.0%} hit, {r['calls']} calls)" if r.get("calls", 0) >= 3 else ""
                out.append(f"• {who(c)}{rec} at {usd(c.get('mcap0'))} → peak {xfmt(peak)} · {dur(time.time() - c['ts'])} ago")
        out += ["", f"<a href=\"https://dexscreener.com/solana/{ca}\">Chart</a> · <a href=\"https://pump.fun/coin/{ca}\">pump.fun</a>"]
        return "\n".join(out)

    # ── циклы ──
    def engine_tick(self):
        active = self._active()
        self.live_peaks(active)
        self.snapshot_new_calls(active)
        msgs = self.achievements(active) + self.signals(active)
        first = not self.st["primed"]
        if first:
            with self.lock:
                self.st["primed"] = True
        for t in msgs:
            self.broadcast(t)
        if msgs or first:
            self._save()

    def _engine_loop(self):
        n = 0
        while not self._stop.wait(ENGINE_EVERY_S):
            try:
                self.engine_tick()
                n += 1
                if n % 60 == 0:
                    self._save()
            except Exception as e:
                self._err(e)

    def _poll_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                j = self.api("getUpdates", http_timeout=40.0, offset=self.st["offset"], timeout=30,
                             allowed_updates=["message", "channel_post"])
                if not j.get("ok"):
                    raise RuntimeError(str(j.get("description"))[:160])
                for u in j.get("result", []):
                    self.st["offset"] = u["update_id"] + 1
                    self.stats["updates"] += 1
                    msg = u.get("message") or u.get("channel_post")
                    if msg:
                        try:
                            self.handle(msg)
                        except Exception as e:
                            self._err(e)
                backoff = 1.0
            except Exception as e:
                self._err(e)
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def start(self):
        if not enabled() or self._threads:
            return
        try:
            me = self.api("getMe")
            self.bot_user = (me.get("result") or {}).get("username", "")
            self.api("setMyCommands", commands=[
                dict(command="top", description="Best callers by real results"),
                dict(command="hot", description="Biggest calls of the last hours"),
                dict(command="caller", description="A caller's track record"),
                dict(command="card", description="Achievement card of the strongest call today"),
                dict(command="alerts", description="Achievements and signals here: on/off")])
        except Exception as e:
            self._err(e)
        self.stats["state"] = "on"
        for fn, name in ((self._poll_loop, "argus-poll"), (self._engine_loop, "argus-engine")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def snapshot(self) -> dict:
        return dict(self.stats, subs=len(self.st["subs"]), channel=bool(channel()), bot=self.bot_user)
