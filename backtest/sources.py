"""Provider seam for the historical sentiment series (mirrors enrichment.providers).
Fixture now; MCP-approx (Task 8) and REST (Task 9) added later."""

import json
from pathlib import Path
from typing import Protocol

from backtest.series import SentimentSeries, load_sentiment_series


class SentimentSource(Protocol):
    name: str

    def series(self, ticker: str) -> SentimentSeries: ...


class FixtureSentimentSource:
    name = "fixture"

    def __init__(self, fixture_dir: str):
        self._dir = Path(fixture_dir)

    def series(self, ticker: str) -> SentimentSeries:
        path = self._dir / f"sentiment_{ticker}.json"
        if not path.exists():
            return SentimentSeries(ticker=ticker, points=[])
        return load_sentiment_series(json.loads(path.read_text(encoding="utf-8")))
