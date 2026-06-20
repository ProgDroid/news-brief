# backtest/evaluation.py
"""Discovery-vs-confirmation discipline: split-sample, best-horizon pick, and a
multiple-comparison caveat string for the report."""


def split_pairs(pairs: list[tuple[float, float]], frac: float = 0.5):
    cut = int(len(pairs) * frac)
    return pairs[:cut], pairs[cut:]


def best_horizon(ic_by_horizon: dict[int, float]) -> int:
    return max(ic_by_horizon, key=lambda h: abs(ic_by_horizon[h]))


def bonferroni_note(num_horizons: int, alpha: float = 0.05) -> str:
    adj = alpha / num_horizons if num_horizons else alpha
    return (
        f"Discovery over {num_horizons} horizons; treat any single p<{alpha} "
        f"with a Bonferroni-adjusted threshold alpha/N={adj:g}. Confirm the "
        f"chosen horizon on held-out data before any sizing decision."
    )
