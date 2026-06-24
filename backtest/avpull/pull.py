# backtest/avpull/pull.py
"""Resumable Alpha Vantage puller. Pure helpers (CI-tested) + live fetch/run
(operator-run, network — NOT in CI, like prices_yf / scorer_llm). The live
functions are added in a later task; this file currently holds only the pure
helpers."""


def is_throttled(resp: dict) -> bool:
    return any(k in resp for k in ("Information", "Note", "Error Message"))


def quarters_2010_to(end_year: int, end_q: int) -> list[str]:
    out: list[str] = []
    for y in range(2010, end_year + 1):
        for q in range(1, 5):
            if y == end_year and q > end_q:
                break
            out.append(f"{y}Q{q}")
    return out


def pending_units(
    universe: list[str], quarters: list[str], done: set[tuple]
) -> list[tuple]:
    units: list[tuple] = [("news", t) for t in universe]
    for t in universe:
        for q in quarters:
            units.append(("transcript", t, q))
    return [u for u in units if u not in done]
