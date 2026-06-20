import pytest

from backtest.scorer_llm import build_scoring_prompt, parse_score


def test_build_prompt_includes_headlines_and_scale():
    p = build_scoring_prompt("MU", "2025-09", ["Micron beats", "Pricing surges"])
    assert "MU" in p and "2025-09" in p and "-1" in p and "Micron beats" in p


def test_parse_score_reads_and_clamps():
    assert parse_score("SCORE: 0.42") == 0.42
    assert parse_score("0.9") == 0.9
    assert parse_score("1.8") == 1.0  # clamped
    with pytest.raises(ValueError):
        parse_score("no number here")
