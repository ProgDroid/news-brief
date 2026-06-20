# tests/test_backtest_metrics.py
import math

import pytest

from backtest.metrics import hit_rate, quantile_returns, spearman_rank_ic


def test_spearman_perfect_monotonic():
    pairs = [(1.0, 0.01), (2.0, 0.02), (3.0, 0.03), (4.0, 0.04)]
    assert round(spearman_rank_ic(pairs), 6) == 1.0
    assert round(spearman_rank_ic([(1, -1), (2, -2), (3, -3)]), 6) == -1.0


def test_spearman_degenerate_returns_zero():
    assert spearman_rank_ic([(1.0, 0.5)]) == 0.0
    assert spearman_rank_ic([(1.0, 0.1), (1.0, 0.2)]) == 0.0  # zero variance in x


def test_quantile_returns_monotone_buckets():
    pairs = [(float(i), float(i) / 100) for i in range(10)]
    qr = quantile_returns(pairs, q=2)
    assert len(qr) == 2
    assert qr[0] < qr[1]


def test_quantile_returns_rejects_q_below_one():
    # q < 1 is meaningless (no buckets) and previously returned [] silently.
    with pytest.raises(ValueError):
        quantile_returns([(1.0, 0.01), (2.0, 0.02)], q=0)
    with pytest.raises(ValueError):
        quantile_returns([(1.0, 0.01)], q=-3)


def test_hit_rate_counts_sign_agreement():
    pairs = [(0.5, 0.02), (0.5, -0.02), (-0.5, -0.01), (0.0, 0.05)]
    assert math.isclose(hit_rate(pairs), 2 / 3)


def test_hit_rate_undefined_is_nan_not_zero():
    # No relevant (non-zero) pairs => undefined, must be NaN, NOT 0.0
    # (0.0 would falsely read as "0% agreement").
    assert math.isnan(hit_rate([]))
    assert math.isnan(hit_rate([(0.0, 0.05), (0.5, 0.0)]))
