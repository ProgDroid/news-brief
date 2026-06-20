# tests/test_backtest_evaluation.py
from backtest.evaluation import best_horizon, bonferroni_note, split_pairs


def test_split_pairs_chronological_halves():
    pairs = [(i, i) for i in range(10)]
    disc, hold = split_pairs(pairs, frac=0.6)
    assert disc == [(i, i) for i in range(6)]
    assert hold == [(i, i) for i in range(6, 10)]


def test_best_horizon_picks_max_abs_ic():
    assert best_horizon({1: 0.02, 5: -0.11, 21: 0.05}) == 5


def test_bonferroni_note_reports_adjusted_alpha():
    note = bonferroni_note(5, alpha=0.05)
    assert "0.01" in note  # 0.05 / 5
