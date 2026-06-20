# backtest/metrics.py
"""Backtest metrics — Spearman rank IC, quantile forward returns, hit rate.
Pure stdlib (no numpy/pandas)."""

import math


def _avg_ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def spearman_rank_ic(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 2:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def quantile_returns(pairs: list[tuple[float, float]], q: int = 5) -> list[float]:
    if not pairs:
        return [float("nan")] * q
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    out: list[float] = []
    for b in range(q):
        lo = b * n // q
        hi = (b + 1) * n // q
        bucket = ordered[lo:hi]
        out.append(sum(p[1] for p in bucket) / len(bucket) if bucket else float("nan"))
    return out


def hit_rate(pairs: list[tuple[float, float]]) -> float:
    rel = [(s, r) for s, r in pairs if s != 0 and r != 0]
    if not rel:
        return 0.0
    agree = sum(1 for s, r in rel if (s > 0) == (r > 0))
    return agree / len(rel)
