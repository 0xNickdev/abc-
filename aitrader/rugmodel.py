"""Модель рагов для рынка от $200k (17.10): вероятность, что токен за 4 ч упадёт на −50%+.

Обучена на датасете learn (src="mid"): train 15.10 → test 16.10 AUC 0.90; отсечение 30% самых
рискованных молодых токенов: доля рагов 6.7% → 0.2%, EV симуляции +1.4% → +3.9%/сделку.
Признаки — те же, что пишет learn: DexScreener (капа, ликвидность, объём, сделки, изменения,
возраст) + окна потока 30с/1м/5м + обвал за минуту/откат от пика/ликвидность пула.
Файл models/rug_mid.pkl (обучение — scripts/train_rug_mid.py); нет файла/sklearn — фильтр выкл.
"""
from __future__ import annotations

import math
import pathlib
import pickle

DK = ["mcap", "liq", "vol_h1", "vol_h24", "buys_h1", "sells_h1", "chg_h1", "chg_h6", "chg_h24", "age_h"]
WK = ("b", "s", "br", "vbr", "net", "vol", "uniq")
FK = ("crash_1m_max_pct", "dd_from_high_pct", "liq_sol")
PATH = pathlib.Path(__file__).resolve().parent / "models" / "rug_mid.pkl"


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else math.nan


def features(dex: dict, flow: dict) -> list[float]:
    d, f = dex or {}, flow or {}
    v = [_num(d.get(k)) for k in DK]
    for w in ("30s", "1m", "5m"):
        ww = f.get(w) or {}
        v += [_num(ww.get(k)) for k in WK]
    v += [_num(f.get(k)) for k in FK]
    return v


_MODEL = None
_LOADED = False


def load():
    global _MODEL, _LOADED
    _LOADED = True
    try:
        with open(PATH, "rb") as fh:
            _MODEL = pickle.load(fh)
    except Exception:
        _MODEL = None
    return _MODEL


def score(dex: dict, flow: dict) -> float | None:
    """Вероятность рага; None — модели нет (фильтр не применяется)."""
    if not _LOADED:
        load()
    if not _MODEL:
        return None
    try:
        import numpy as np
        x = np.array([features(dex, flow)])[:, _MODEL["keep"]]
        return float(_MODEL["clf"].predict_proba(x)[0, 1])
    except Exception:
        return None


def threshold() -> float | None:
    if not _LOADED:
        load()
    return _MODEL.get("threshold") if _MODEL else None
