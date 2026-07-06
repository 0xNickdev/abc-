"""Детектор ферм по кластеризации истории кошельков (главная независимая проверка).

Идея юзера: у рискованного токена (низкая MK, мало холдеров) взять топ-покупателей по
объёму, у каждого вытащить историю торгов через GMGN и найти кошельки с ОДИНАКОВОЙ
историей — фермы часто крутят одни и те же прошлые токены с новых и старых кошей.
Много кошельков с пересекающейся историей = ферма (красный флаг качества).

Ядро (`cluster_wallets`) — чистая математика на множествах токенов, тестируется без сети.
`detect` берёт данные через инъекцию (get_traders/get_activity) → app подставляет gmgn-cli.
Дорого по rate-limit (N кошельков × portfolio activity) → on-demand, бюджет по max_wallets.
"""
from __future__ import annotations

from collections import defaultdict


def cluster_wallets(histories: dict[str, set], min_common: int = 2,
                    farm_min: int = 3) -> dict:
    """Сгруппировать кошельки, чьи наборы торгованных токенов пересекаются на ≥min_common.
    Union-find по попарному пересечению → крупнейший кластер = сила фермы. red_flag при
    кластере ≥ farm_min (столько кошельков с общей историей — уже не совпадение)."""
    wallets = [w for w, h in histories.items() if h]        # без пустых историй
    parent = {w: w for w in wallets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(wallets):
        for b in wallets[i + 1:]:
            if len(histories[a] & histories[b]) >= min_common:
                union(a, b)

    groups: dict[str, list] = defaultdict(list)
    for w in wallets:
        groups[find(w)].append(w)
    clusters = sorted((g for g in groups.values() if len(g) >= 2), key=len, reverse=True)
    largest = len(clusters[0]) if clusters else 1
    checked = len(histories)
    return dict(
        checked=checked,
        clustered=sum(len(c) for c in clusters),
        clusters=[len(c) for c in clusters],
        largest_cluster=largest,
        farm_ratio=round(sum(len(c) for c in clusters) / checked, 3) if checked else 0.0,
        red_flag=largest >= farm_min,
        sample=[sorted(c)[:5] for c in clusters[:3]],           # примеры кошельков кластеров
    )


def detect(address: str, get_traders, get_activity, *,
           max_wallets: int = 30, min_common: int = 2, farm_min: int = 3) -> dict:
    """Полный прогон: топ-покупатели → история каждого → кластеризация. get_traders(address,
    n)->[wallet]; get_activity(wallet)->iterable[token_addr]. Любой сбой источника = пропуск
    кошелька, не падаем. Пусто, если не набрали кошельков."""
    try:
        wallets = list(get_traders(address, max_wallets) or [])[:max_wallets]
    except Exception:
        return {}
    histories: dict[str, set] = {}
    for w in wallets:
        if not w:
            continue
        try:
            toks = set(get_activity(w) or [])
        except Exception:
            toks = set()
        toks.discard(address)                                  # текущий токен не считаем общим
        histories[w] = toks
    if not histories:
        return {}
    return cluster_wallets(histories, min_common, farm_min)


# ── Фронтранеры/боты среди топ-покупателей (по тегам GMGN) ──
FRONTRUN_TAGS = {"sniper", "rat_trader", "bundler", "dex_bot", "frontrun", "front_run", "mev"}


def _trader_tags(t: dict) -> list:
    """Теги трейдера из строки GMGN (поле варьирует: tags/maker_token_tags/wallet_tag_v2/tag_rank)."""
    for k in ("tags", "maker_token_tags", "wallet_tag_v2", "tag_rank", "tag"):
        v = t.get(k)
        if isinstance(v, list):
            return [str(x).lower() for x in v]
        if isinstance(v, dict):
            return [str(x).lower() for x in v.keys()]
        if isinstance(v, str) and v:
            return [v.lower()]
    return []


def frontrunners(traders: list, tags: set | None = None) -> dict:
    """Доля фронтранеров/снайперов/ботов среди топ-покупателей (по тегам GMGN).
    red_flag при доле ≥30% — токен фармят боты/снайперы, органики мало."""
    tags = tags or FRONTRUN_TAGS
    checked = hits = 0
    kinds: dict[str, int] = {}
    for t in traders or []:
        if not isinstance(t, dict):
            continue
        checked += 1
        hit = [x for x in _trader_tags(t) if x in tags]
        if hit:
            hits += 1
            for x in hit:
                kinds[x] = kinds.get(x, 0) + 1
    return dict(checked=checked, frontrunners=hits, by_tag=kinds,
                ratio=round(hits / checked, 3) if checked else 0.0,
                red_flag=(checked > 0 and hits / checked >= 0.3))
