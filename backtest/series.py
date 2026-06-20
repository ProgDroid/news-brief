# backtest/series.py
"""Data contract for the backtest: a sentiment series + a price series.
Pure dataclasses, stdlib only — vendor- and source-agnostic."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SentimentPoint:
    date: str  # ISO YYYY-MM-DD
    value: float


@dataclass(frozen=True)
class SentimentSeries:
    ticker: str
    points: tuple[SentimentPoint, ...]

    def __post_init__(self):
        # frozen=True blocks reassigning the field but not mutating a list in
        # place; coerce to a tuple so the series is genuinely immutable.
        if not isinstance(self.points, tuple):
            object.__setattr__(self, "points", tuple(self.points))

    def dates(self) -> list[str]:
        return sorted(p.date for p in self.points)

    def as_of_map(self) -> dict[str, float]:
        return {p.date: p.value for p in self.points}


@dataclass(frozen=True)
class PriceSeries:
    ticker: str
    closes: dict[str, float]  # ISO date -> adjusted close


def load_sentiment_series(d: dict) -> SentimentSeries:
    return SentimentSeries(
        ticker=d["ticker"],
        points=[
            SentimentPoint(p["date"], float(p["value"])) for p in d.get("points", [])
        ],
    )


def load_price_series(d: dict) -> PriceSeries:
    return PriceSeries(
        ticker=d["ticker"],
        closes={k: float(v) for k, v in d.get("closes", {}).items()},
    )
