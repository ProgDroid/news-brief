"""Interim sentiment scorer for the MCP-approx pilot: Claude scores the media
tone of a window's headlines into [-1, 1]. DIRECTIONAL approximation only —
NOT a substitute for RavenPack sentiment (see Task 9)."""

import re

_PROMPT = (
    "You are scoring MEDIA TONE (not a price forecast) for {ticker} during "
    "{window}. Given these headlines, return ONE number in [-1, 1] where -1 is "
    "strongly negative tone and +1 strongly positive. Reply with only:\n"
    "SCORE: <number>\n\nHeadlines:\n{headlines}"
)


def build_scoring_prompt(ticker: str, window_label: str, headlines: list[str]) -> str:
    joined = "\n".join(f"- {h}" for h in headlines)
    return _PROMPT.format(ticker=ticker, window=window_label, headlines=joined)


def parse_score(text: str) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        raise ValueError(f"no parseable score in: {text!r}")
    return max(-1.0, min(1.0, float(m.group())))


def score_window(client, ticker: str, window_label: str, headlines: list[str]) -> float:
    # client: an anthropic.Anthropic() instance (reuse the brief's client/config).
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": build_scoring_prompt(ticker, window_label, headlines),
            }
        ],
    )
    return parse_score(resp.content[0].text)
