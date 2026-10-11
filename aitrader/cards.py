"""Карточки Argus: анимированный GIF к ачивке (как у SpyDefi, но свой стиль).

Кадр 960×540: тёмное поле с сеткой, знак Argus (глаз — своя векторная графика, без эмодзи),
крупный икс считает вверх, справа путь капы от колла до пика рисуется со свечением.
Кривая — стилизация пути (форма из контракта как seed), подписаны только факты: капа на
колле и пик. Рендер в 2× и сжатие (сглаживание), ~20 кадров, последний держится.
Шрифты в assets/fonts (SIL OFL): Space Grotesk, JetBrains Mono.
"""
from __future__ import annotations

import functools
import hashlib
import io
import math
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H, S = 960, 540, 2               # итоговый размер и множитель суперсэмплинга
FONTS = pathlib.Path(__file__).resolve().parent / "assets" / "fonts"
BG = (6, 7, 11)
GRID = (20, 23, 32)
TEXT = (236, 239, 245)
MUTED = (122, 130, 150)
FRAMES = 20


def accent(level: float) -> tuple[int, int, int]:
    if level >= 20:
        return (255, 200, 87)       # золото
    if level >= 5:
        return (164, 123, 255)      # фиолетовый
    return (79, 209, 255)           # голубой


@functools.lru_cache(maxsize=32)
def font(name: str, size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONTS / f"{name}.ttf"), size * S)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def _eye(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, col, t: float):
    """Знак Argus: миндалевидный глаз из двух дуг + радужка, зрачок «сканирует»."""
    pts_top, pts_bot = [], []
    for i in range(41):
        a = -1 + 2 * i / 40
        y = r * 0.55 * (1 - a * a)
        pts_top.append((cx + a * r, cy - y))
        pts_bot.append((cx + a * r, cy + y))
    d.line(pts_top, fill=col, width=int(2.2 * S), joint="curve")
    d.line(pts_bot, fill=col, width=int(2.2 * S), joint="curve")
    ir = r * 0.36
    d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], outline=col, width=int(2.2 * S))
    px = cx + math.sin(t * math.pi * 2) * ir * 0.35
    pr = ir * 0.42
    d.ellipse([px - pr, cy - pr, px + pr, cy + pr], fill=col)


def _curve(seed: bytes, n: int = 60) -> list[float]:
    """Стилизованный путь 0→1: рост с откатами (форма из seed — у каждого токена своя)."""
    h = hashlib.sha256(seed).digest()
    ys = []
    for i in range(n):
        x = i / (n - 1)
        base = x ** 1.8
        wob = sum(math.sin(x * math.pi * (3 + k) + h[k] / 40) * (h[k + 8] / 255) * 0.06 for k in range(4))
        ys.append(min(1.0, max(0.0, base + wob * (1 - x) * 1.4)))
    ys[0], ys[-1] = 0.0, 1.0
    return ys


def _ease(t: float) -> float:
    return 1 - (1 - t) ** 3


def _usd(v: float) -> str:
    if v <= 0:
        return "—"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.1f}{suf}".replace(".0" + suf, suf)
    return f"${v:.0f}"


def _x(m: float) -> str:
    return f"x{m:.1f}" if m < 10 else f"x{m:.0f}"


def _base(level: int, symbol: str, caller: str, mc0: float, mc_peak: float, took: str) -> Image.Image:
    col = accent(level)
    im = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(im)
    for gx in range(0, W * S, 48 * S):
        d.line([(gx, 0), (gx, H * S)], fill=GRID, width=S)
    for gy in range(0, H * S, 48 * S):
        d.line([(0, gy), (W * S, gy)], fill=GRID, width=S)
    # мягкое пятно акцента за цифрой (screen-смешение: только подсветка, без грязи)
    glow = Image.new("RGB", (W * S, H * S), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([40 * S, 110 * S, 520 * S, 470 * S], fill=tuple(c // 4 for c in col))
    im = ImageChops.screen(im, glow.filter(ImageFilter.GaussianBlur(80 * S)))
    d = ImageDraw.Draw(im)
    d.text((86 * S, 44 * S), "ARGUS", font=font("SpaceGrotesk", 26), fill=TEXT, anchor="lm")
    d.text((W * S - 40 * S, 44 * S), "ACHIEVEMENT UNLOCKED", font=font("JetBrainsMono", 15, "Bold"), fill=col, anchor="rm")
    d.text((44 * S, 360 * S), f"${symbol}"[:16], font=font("SpaceGrotesk", 40), fill=TEXT, anchor="ls")
    d.text((44 * S, 408 * S), f"{_usd(mc0)}  →  {_usd(mc_peak)}", font=font("SpaceGrotesk", 30, "Medium"), fill=col, anchor="ls")
    d.text((44 * S, 448 * S), f"called by {caller}"[:40], font=font("JetBrainsMono", 17, "Medium"), fill=MUTED, anchor="ls")
    if took:
        d.text((44 * S, 476 * S), f"peak in {took}", font=font("JetBrainsMono", 17, "Medium"), fill=MUTED, anchor="ls")
    d.text((W * S - 40 * S, (H - 30) * S), "on-chain call analytics", font=font("JetBrainsMono", 13, "Regular"),
           fill=MUTED, anchor="rs")
    return im


def achievement_gif(level: int, mult: float, symbol: str, caller: str, mc0: float, mc_peak: float,
                    took: str = "", seed: str = "") -> bytes:
    """GIF-ачивка: икс считает до mult, кривая дорисовывается к пику, глаз сканирует."""
    col = accent(level)
    base = _base(level, symbol, caller, mc0, mc_peak, took)
    ys = _curve((seed or symbol).encode())
    # панель графика справа
    x0, x1, y0, y1 = 540 * S, 920 * S, 430 * S, 120 * S
    frames = []
    for f in range(FRAMES):
        t = _ease(f / (FRAMES - 1))
        im = base.copy()
        d = ImageDraw.Draw(im)
        _eye(d, 56 * S, 44 * S, 20 * S, col, f / FRAMES)
        # пунктир капы колла + подписи
        for xx in range(x0, x1, 14 * S):
            d.line([(xx, y0), (xx + 7 * S, y0)], fill=MUTED, width=S)
        d.text((x0, y0 + 26 * S), f"CALL {_usd(mc0)}", font=font("JetBrainsMono", 14, "Bold"), fill=MUTED, anchor="ls")
        k = max(2, int(len(ys) * t))
        pts = [(x0 + (x1 - x0) * i / (len(ys) - 1), y0 + (y1 - y0) * ys[i]) for i in range(k)]
        layer = Image.new("RGB", im.size, (0, 0, 0))
        ImageDraw.Draw(layer).line(pts, fill=col, width=int(7 * S), joint="curve")
        im = ImageChops.screen(im, layer.filter(ImageFilter.GaussianBlur(10 * S)))
        d = ImageDraw.Draw(im)
        d.line(pts, fill=col, width=int(3 * S), joint="curve")
        hx, hy = pts[-1]
        d.ellipse([hx - 7 * S, hy - 7 * S, hx + 7 * S, hy + 7 * S], fill=TEXT)
        if f == FRAMES - 1:
            d.text((x1, y1 - 18 * S), f"PEAK {_usd(mc_peak)}", font=font("JetBrainsMono", 14, "Bold"), fill=TEXT, anchor="rs")
        cur = 1 + (mult - 1) * t
        d.text((40 * S, 300 * S), _x(cur), font=font("SpaceGrotesk", 150), fill=TEXT, anchor="ls")
        frames.append(im.resize((W, H), Image.LANCZOS).quantize(colors=128, method=Image.Quantize.MEDIANCUT,
                                                                 dither=Image.Dither.NONE))
    buf = io.BytesIO()
    durs = [55] * (FRAMES - 1) + [4000]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durs, loop=0,
                   optimize=True, disposal=1)
    return buf.getvalue()
