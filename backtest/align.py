# backtest/align.py
"""Align a sentiment series with forward returns; build the level/Δ pair lists
and the event-conditioned slice."""

from backtest.series import SentimentPoint, SentimentSeries


def align(
    sentiment: SentimentSeries, fwd: dict[str, dict[int, float]], horizon: int
) -> list[tuple[float, float]]:
    smap = sentiment.as_of_map()
    pairs = []
    for d in sentiment.dates():
        if d in fwd and horizon in fwd[d]:
            pairs.append((smap[d], fwd[d][horizon]))
    return pairs


def to_delta(sentiment: SentimentSeries) -> SentimentSeries:
    dates = sentiment.dates()
    smap = sentiment.as_of_map()
    pts = [
        SentimentPoint(dates[i], smap[dates[i]] - smap[dates[i - 1]])
        for i in range(1, len(dates))
    ]
    return SentimentSeries(ticker=sentiment.ticker, points=pts)


def event_filtered(
    pairs_by_date: list[tuple[str, float, float]],
    event_dates: set[str],
    window: int,
) -> list[tuple[float, float]]:
    """Keep rows within `window` positions of an event-dated row.

    Precondition: `pairs_by_date` MUST be ordered by date ascending — the
    window is POSITIONAL, so unsorted input yields wrong neighbors.
    """
    dates = [d for d, _, _ in pairs_by_date]
    keep_idx: set[int] = set()
    for i, d in enumerate(dates):
        if d in event_dates:
            for j in range(max(0, i - window), min(len(dates), i + window + 1)):
                keep_idx.add(j)
    return [(s, r) for k, (_, s, r) in enumerate(pairs_by_date) if k in keep_idx]
