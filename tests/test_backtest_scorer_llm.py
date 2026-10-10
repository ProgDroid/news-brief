import pytest

from backtest.scorer_llm import build_scoring_prompt, parse_score, score_window


def test_build_prompt_includes_headlines_and_scale():
    p = build_scoring_prompt("MU", "2025-09", ["Micron beats", "Pricing surges"])
    assert "MU" in p and "2025-09" in p and "-1" in p and "Micron beats" in p


class _Block:
    def __init__(self, type, text=""):
        self.type = type
        self.text = text


class _Client:
    def __init__(self, content):
        self.kwargs = None
        self.messages = self
        self._content = content

    def create(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"content": self._content})()


def test_score_window_runs_haiku_5_5_with_thinking_off():
    """news-brief-c5k. Haiku 5.5 thinks by default; with max_tokens=16 the
    thinking would consume the budget before the score is written."""
    client = _Client([_Block("text", "0.3")])
    assert score_window(client, "MU", "2025-09", ["h"]) == 0.3
    assert client.kwargs["model"] == "claude-haiku-5-5"
    assert client.kwargs["thinking"] == {"type": "disabled"}


def test_score_window_reads_the_text_block_not_the_first_block():
    client = _Client([_Block("thinking"), _Block("text", "-0.5")])
    assert score_window(client, "MU", "2025-09", ["h"]) == -0.5


def test_parse_score_reads_and_clamps():
    assert parse_score("SCORE: 0.42") == 0.42
    assert parse_score("0.9") == 0.9
    assert parse_score("1.8") == 1.0  # clamped
    with pytest.raises(ValueError):
        parse_score("no number here")
