"""Smart-money 跟踪钱包 —— 信号/共识用，绝非复制跟单（copytrade）。

定位：把"你长期盈利的钱包是否在某币里"作为一个【更强的共识信号】喂进评分，
入场与执行仍由我们自己的快速通道完成（不是镜像别人的成交）。

数据来源（隐私）：outputs/wallets.json（用户私有 alpha 列表，已 gitignore，不入库）。
用法：
  confluence(holder_addrs) → 命中的跟踪钱包列表（"几只聪明钱在场"）。
  把某 token 的 holders 列表喂进来即可；真实 holders 由适配器在需要时拉取。

⚠️ rolling_pnl 仍是占位：上线前必须用链上/数据 API 按 30~60 天窗口校验每个钱包的
   已实现盈亏/胜率，否则会把"最近走运的钱包"当成有 edge（survivorship bias）。
"""
from __future__ import annotations

import json
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
WALLETS_PATH = HERE / "outputs" / "wallets.json"

# Веса «доказанного эджа» кошельков считает офлайн review-loop (review.py) из
# реализованных исходов и кладёт сюда; scoring читает их через edge_weight().
# Путь уважает ABC_DATA_DIR (том Railway), как и остальной журнал/позиции.
_DATA_DIR = pathlib.Path(os.getenv("ABC_DATA_DIR").strip()) if os.getenv("ABC_DATA_DIR", "").strip() else HERE / "outputs"
EDGES_PATH = _DATA_DIR / "wallet_edges.json"


def load_tracked() -> dict:
    """读私有跟踪钱包表 → {address: {name, emoji, groups}}。缺失/损坏则空。"""
    if not WALLETS_PATH.exists():
        return {}
    try:
        data = json.loads(WALLETS_PATH.read_text())
        if isinstance(data, list):
            return {w["address"]: w for w in data if isinstance(w, dict) and w.get("address")}
    except Exception:
        pass
    return {}


TRACKED = load_tracked()


def reload_tracked() -> dict:
    global TRACKED
    TRACKED = load_tracked()
    return TRACKED


def is_tracked(addr: str) -> bool:
    return addr in TRACKED


def get(addr: str) -> dict:
    return TRACKED.get(addr, {})


def confluence(holder_addrs) -> list:
    """holder 地址 ∩ 跟踪集 → [{address,name,emoji}]（去重保序）。
    返回的长度即"在场的聪明钱数量"，是比 GMGN 通用 smart_degen 更精准的私有信号。"""
    seen, out = set(), []
    for a in holder_addrs or []:
        if a in TRACKED and a not in seen:
            seen.add(a)
            w = TRACKED[a]
            out.append(dict(address=a, name=w.get("name", ""), emoji=w.get("emoji", "")))
    return out


def rolling_pnl(addr: str) -> dict:
    """占位：每个钱包的滚动已实现盈亏/胜率。上线前接真实数据再启用做权重。"""
    return dict(address=addr, window_days=30, realized_pnl=None, win_rate=None, trades=None)


# ── Edge-weighting: вес кошелька по ДОКАЗАННОМУ эджу (заполняет review-loop) ──
# Ключ = имя кошелька (то, что течёт в features.tracked_names → attrib → review),
# чтобы вес был доступен там же, где scoring считает бонус за smart-money в场.
_edges_mem: dict | None = None


def load_edges() -> dict:
    """Читает outputs/wallet_edges.json → {name: {weight, trades, winrate, expectancy_R}}.
    Кэш в памяти; save_edges/reload_edges инвалидируют. Файла нет → пусто (все веса нейтральны)."""
    global _edges_mem
    if _edges_mem is None:
        try:
            data = json.loads(EDGES_PATH.read_text())
            _edges_mem = data if isinstance(data, dict) else {}
        except Exception:
            _edges_mem = {}
    return _edges_mem


def save_edges(edges: dict):
    """Ночной цикл кладёт сюда посчитанные веса; обновляет и кэш, и файл."""
    global _edges_mem
    _edges_mem = dict(edges or {})
    try:
        EDGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        EDGES_PATH.write_text(json.dumps(_edges_mem, ensure_ascii=False))
    except Exception:
        pass


def reload_edges() -> dict:
    global _edges_mem
    _edges_mem = None
    return load_edges()


LEARN_PATH = _DATA_DIR / "self_learn.json"
_learn_on = None


def learning_enabled() -> bool:
    """Включён ли edge-weighting самообучения. Персист self_learn.json; env ABC_SELF_LEARN=1 —
    дефолт-он. По умолчанию ВЫКЛ: не учимся на шуме, пока мало данных."""
    global _learn_on
    if _learn_on is None:
        try:
            _learn_on = bool(json.loads(LEARN_PATH.read_text()).get("enabled"))
        except Exception:
            _learn_on = os.getenv("ABC_SELF_LEARN", "").strip().lower() in ("1", "true", "yes", "on")
    return _learn_on


def set_learning(on: bool) -> bool:
    global _learn_on
    _learn_on = bool(on)
    try:
        LEARN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEARN_PATH.write_text(json.dumps({"enabled": _learn_on}))
    except Exception:
        pass
    return _learn_on


def edge_weight(name: str) -> float:
    """Множитель бонуса за этот кошелёк в scoring. 1.0 = нейтрально (выключено / мало данных /
    неизвестен) — так фича без данных не искажает ранжирование; review-loop поднимает вес
    доказанно прибыльным кошелькам и режет убыточным (см. review.wallet_edge)."""
    if not learning_enabled():
        return 1.0
    e = load_edges().get(name)
    if not isinstance(e, dict):
        return 1.0
    try:
        return float(e.get("weight", 1.0))
    except (TypeError, ValueError):
        return 1.0


def summary() -> dict:
    from collections import Counter
    groups = Counter(x for w in TRACKED.values() for x in (w.get("groups") or ["(none)"]))
    sample = [dict(name=w.get("name", ""), emoji=w.get("emoji", ""),
                   address=a[:4] + "…" + a[-4:]) for a, w in list(TRACKED.items())[:8]]
    return dict(total=len(TRACKED), groups=dict(groups), sample=sample)
