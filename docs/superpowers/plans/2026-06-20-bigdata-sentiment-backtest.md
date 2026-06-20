# Bigdata.com Sentiment Backtest (Feature B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decide — with evidence — whether Bigdata.com (RavenPack) entity sentiment and events earn the right to drive position *sizing*, by building a vendor-agnostic backtest engine (rank IC + quantile forward returns + hit rate, with an event-conditioned slice and a horizon sweep) and feeding it a historical sentiment series.

**Architecture:** A new offline-only `backtest/` package, separate from the cron pipeline. Pure-Python metrics operate on a small, explicit data contract (a sentiment series + a price series), so the engine is fully unit-testable against synthetic fixtures with **no live calls**. The historical sentiment series enters through a provider seam (`SentimentSource`) mirroring the shipped `enrichment` provider pattern — three implementations: fixture (tests), MCP-approx (interim pilot, LLM-scored), and REST (faithful, deferred until the business-email key lands).

**Tech Stack:** Python 3 (stdlib only for the engine — no numpy/pandas, matching this repo's dependency-light style), the existing Anthropic client for the interim LLM scorer, `yfinance` for historical prices (thin, manually-verified adapter), ruff + pytest gate.

## Why this plan differs from the 2026-06-19 design spec

The spec assumed the historical sentiment series would be **reconstructed from time-windowed `bigdata_search`**. A live MCP probe on 2026-06-20 (see memory `bigdata-enrichment-validated-live`) disproved the naive form of that:

- `bigdata_search` results expose **no numeric sentiment** — only chunk text + relevance. (Confirmed in both smart and fast mode.)
- The `sentiment` *filter* operates on **per-chunk forecasted market impact**, not entity media-tone, and surfaces **no total count** — so bucket-counting is biased and uncomputable.
- The `bigdata_sentiment_tearsheet` (which *does* return numeric entity sentiment + baseline + z-scores) is **current-only** (90-day lookback). The MCP connector exposes **no historical-sentiment time-series tool**.
- Timestamp-windowed retrieval itself works and the archive is ≥9 months deep; cost ≈ **1.2 query units per windowed search**.

**Consequence:** a *faithful* historical entity-sentiment series requires RavenPack's purpose-built sentiment dataset via the **REST API** → the business-email REST key is a **hard prerequisite for the faithful backtest** (Task 9), not just a production last-mile. Until it lands, the engine is validated on fixtures (Tasks 1–7) and run on an **approximate, LLM-scored** series the MCP path *can* produce (Task 8) for a directional-only read. This sequencing means real engineering proceeds now and the key unblocks only the final faithful run.

## Global Constraints

- **Offline/R&D only — NOT shipped in the cron image.** `backtest/` is analysis code; it is NOT imported by `brief.py` and needs **no** Dockerfile `COPY` or CI workflow-path change (unlike `enrichment/`; contrast memory `dockerfile-copy-allowlist`). Its `tests/` run in CI on full checkout like any other test.
- **No live Bigdata/Anthropic/network calls in CI.** Every engine test runs on synthetic or recorded fixtures. Live calls happen only in the operator-run pilot scripts (Tasks 8–9).
- **Never auto-writes a sizing field.** This work only *decides whether* sentiment may drive sizing. No task wires sentiment into `trading.py` sizing. The deliverable is a go/no-go report.
- **Dependency-light:** engine uses Python stdlib only (no numpy/pandas). `yfinance` is the only new runtime dep and is confined to the price adapter (Task 2b), which is not exercised in CI.
- **Discovery vs confirmation discipline (from spec):** the horizon sweep is discovery; report effect sizes with a multiple-comparison caveat and confirm the best horizon on held-out data (Task 4).
- **Branding:** "Bigdata.com" (RavenPack); link https://bigdata.com in any report.
- **Pre-push gate (unchanged):** `ruff check` + `ruff format --check` + `pytest` all green before every commit (memory `brief-local-run`). Commit straight to `main` (memory `newsbrief-commit-to-main`).

---

### Task 1: Package scaffold + data contracts

**Files:**
- Create: `backtest/__init__.py`
- Create: `backtest/series.py`
- Test: `tests/test_backtest_series.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) SentimentPoint(date: str, value: float)` — `date` is ISO `YYYY-MM-DD`.
  - `@dataclass(frozen=True) SentimentSeries(ticker: str, points: list[SentimentPoint])` with `.as_of_map() -> dict[str, float]` (date→value) and `.dates() -> list[str]` (sorted).
  - `@dataclass(frozen=True) PriceSeries(ticker: str, closes: dict[str, float])` (date→adjusted close).
  - `def load_sentiment_series(d: dict) -> SentimentSeries` and `def load_price_series(d: dict) -> PriceSeries` (JSON→dataclass, mirroring `enrichment.models.*_from_dict`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_series.py
from backtest.series import (
    PriceSeries,
    SentimentPoint,
    SentimentSeries,
    load_price_series,
    load_sentiment_series,
)


def test_sentiment_series_sorts_and_maps():
    s = SentimentSeries(
        ticker="MU",
        points=[SentimentPoint("2025-02-01", 0.2), SentimentPoint("2025-01-01", -0.1)],
    )
    assert s.dates() == ["2025-01-01", "2025-02-01"]
    assert s.as_of_map() == {"2025-01-01": -0.1, "2025-02-01": 0.2}


def test_loaders_roundtrip():
    s = load_sentiment_series(
        {"ticker": "MU", "points": [{"date": "2025-01-01", "value": 0.3}]}
    )
    assert s.points[0].value == 0.3
    p = load_price_series({"ticker": "MU", "closes": {"2025-01-01": 100.0}})
    assert isinstance(p, PriceSeries) and p.closes["2025-01-01"] == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_series.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/__init__.py
"""Offline sentiment-backtest engine (Feature B). NOT part of the cron pipeline."""
```

```python
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
    points: list[SentimentPoint]

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
        points=[SentimentPoint(p["date"], float(p["value"])) for p in d.get("points", [])],
    )


def load_price_series(d: dict) -> PriceSeries:
    return PriceSeries(
        ticker=d["ticker"],
        closes={k: float(v) for k, v in d.get("closes", {}).items()},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_series.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backtest/__init__.py backtest/series.py tests/test_backtest_series.py
git commit -m "feat(backtest): data contract — sentiment + price series"
```

---

### Task 2a: Forward-return math (pure)

**Files:**
- Create: `backtest/returns.py`
- Test: `tests/test_backtest_returns.py`

**Interfaces:**
- Consumes: `backtest.series.PriceSeries`.
- Produces: `def forward_returns(prices: PriceSeries, horizons_days: list[int]) -> dict[str, dict[int, float]]` — returns `{date: {horizon: pct_return}}`, where the return for `date`+`h` is `close[date+h trading days]/close[date] - 1`, computed over the sorted date index (calendar-naive: uses positional offset within available closes). Dates lacking a full forward window are omitted for that horizon.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_returns.py
from backtest.returns import forward_returns
from backtest.series import PriceSeries


def test_forward_returns_positional_offsets():
    # 5 consecutive closes; horizon 1 and 2 trading-day-ahead returns.
    p = PriceSeries(
        ticker="X",
        closes={
            "2025-01-01": 100.0,
            "2025-01-02": 110.0,
            "2025-01-03": 121.0,
            "2025-01-04": 121.0,
            "2025-01-05": 132.0,
        },
    )
    fr = forward_returns(p, [1, 2])
    # 1-day: 2025-01-01 -> 110/100-1 = 0.10
    assert round(fr["2025-01-01"][1], 6) == 0.10
    # 2-day: 2025-01-01 -> 121/100-1 = 0.21
    assert round(fr["2025-01-01"][2], 6) == 0.21
    # last date has no forward window for either horizon
    assert "2025-01-05" not in fr or 1 not in fr["2025-01-05"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_returns.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.returns'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/returns.py
"""Forward returns from a price series, by positional (trading-day) offset.
Pure stdlib — calendar-naive: horizon h means h positions ahead in the sorted
close index, which matches daily-bar data with weekends/holidays already removed."""

from backtest.series import PriceSeries


def forward_returns(
    prices: PriceSeries, horizons_days: list[int]
) -> dict[str, dict[int, float]]:
    dates = sorted(prices.closes)
    out: dict[str, dict[int, float]] = {}
    for i, d in enumerate(dates):
        base = prices.closes[d]
        if base == 0:
            continue
        per_h: dict[int, float] = {}
        for h in horizons_days:
            j = i + h
            if j < len(dates):
                per_h[h] = prices.closes[dates[j]] / base - 1.0
        if per_h:
            out[d] = per_h
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_returns.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/returns.py tests/test_backtest_returns.py
git commit -m "feat(backtest): forward-return math (positional offsets)"
```

---

### Task 2b: Historical price adapter (yfinance; manually verified, not in CI)

**Files:**
- Create: `backtest/prices_yf.py`
- Test: `tests/test_backtest_prices_yf.py` (offline — tests the frame→PriceSeries transform only, with a fake frame)

**Interfaces:**
- Produces:
  - `def to_price_series(ticker: str, rows: list[tuple[str, float]]) -> PriceSeries` — pure transform of `(iso_date, adj_close)` rows into a `PriceSeries` (this is what the test covers).
  - `def fetch_price_series(yf_symbol: str, start: str, end: str) -> PriceSeries` — thin `yfinance` call that builds the rows and delegates to `to_price_series`. **Not** exercised in CI (live network); operator verifies manually in Step 5.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_prices_yf.py
from backtest.prices_yf import to_price_series


def test_to_price_series_builds_close_map():
    ps = to_price_series("MU", [("2025-01-01", 100.0), ("2025-01-02", 101.5)])
    assert ps.ticker == "MU"
    assert ps.closes == {"2025-01-01": 100.0, "2025-01-02": 101.5}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_prices_yf.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.prices_yf'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/prices_yf.py
"""Historical daily closes for the backtest. The pure transform is unit-tested;
the live yfinance fetch is operator-run only (no network in CI)."""

from backtest.series import PriceSeries


def to_price_series(ticker: str, rows: list[tuple[str, float]]) -> PriceSeries:
    return PriceSeries(ticker=ticker, closes={d: float(c) for d, c in rows})


def fetch_price_series(yf_symbol: str, start: str, end: str) -> PriceSeries:
    import yfinance as yf  # local import: keeps the dep out of the engine/CI path

    df = yf.download(yf_symbol, start=start, end=end, auto_adjust=True, progress=False)
    rows = [
        (ts.strftime("%Y-%m-%d"), float(row["Close"]))
        for ts, row in df.iterrows()
    ]
    return to_price_series(yf_symbol, rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_prices_yf.py -q`
Expected: PASS.

- [ ] **Step 5: Manual live check (operator, once) + Commit**

Manual: in a PowerShell session (memory `python-via-powershell`), run
`python -c "from backtest.prices_yf import fetch_price_series as f; print(len(f('MU','2025-01-01','2025-03-01').closes))"`
Expected: a positive integer (~40 trading days). If `yfinance` is missing, `pip install yfinance` and note it in the pilot README (Task 8). Do not add it to the brief image.

```bash
git add backtest/prices_yf.py tests/test_backtest_prices_yf.py
git commit -m "feat(backtest): historical price adapter (yfinance, offline transform tested)"
```

---

### Task 3: Core metrics — rank IC, quantile returns, hit rate

**Files:**
- Create: `backtest/metrics.py`
- Test: `tests/test_backtest_metrics.py`

**Interfaces:**
- Produces:
  - `def spearman_rank_ic(pairs: list[tuple[float, float]]) -> float` — Spearman correlation between sentiment and forward return across (sentiment, fwd_return) pairs; ties via average ranks; returns `0.0` for <2 points or zero variance.
  - `def quantile_returns(pairs: list[tuple[float, float]], q: int = 5) -> list[float]` — mean forward return per sentiment quantile (low→high), `q` buckets; bucket with no members → `float("nan")`.
  - `def hit_rate(pairs: list[tuple[float, float]]) -> float` — fraction where `sign(sentiment) == sign(fwd_return)` (zeros excluded from numerator and denominator).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_metrics.py
import math

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
    assert qr[0] < qr[1]  # low-sentiment bucket has lower mean fwd return


def test_hit_rate_counts_sign_agreement():
    pairs = [(0.5, 0.02), (0.5, -0.02), (-0.5, -0.01), (0.0, 0.05)]
    # 3 non-zero-sentiment pairs; agree: (0.5,0.02) yes, (0.5,-0.02) no, (-0.5,-0.01) yes
    assert math.isclose(hit_rate(pairs), 2 / 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.metrics'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/metrics.py
"""Backtest metrics — Spearman rank IC, quantile forward returns, hit rate.
Pure stdlib (no numpy/pandas)."""

import math


def _avg_ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def spearman_rank_ic(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 2:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def quantile_returns(pairs: list[tuple[float, float]], q: int = 5) -> list[float]:
    if not pairs:
        return [float("nan")] * q
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    out: list[float] = []
    for b in range(q):
        lo = b * n // q
        hi = (b + 1) * n // q
        bucket = ordered[lo:hi]
        out.append(sum(p[1] for p in bucket) / len(bucket) if bucket else float("nan"))
    return out


def hit_rate(pairs: list[tuple[float, float]]) -> float:
    rel = [(s, r) for s, r in pairs if s != 0 and r != 0]
    if not rel:
        return 0.0
    agree = sum(1 for s, r in rel if (s > 0) == (r > 0))
    return agree / len(rel)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_metrics.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backtest/metrics.py tests/test_backtest_metrics.py
git commit -m "feat(backtest): rank IC + quantile returns + hit rate"
```

---

### Task 4: Discovery/confirmation discipline — split-sample + multiple-comparison caveat

**Files:**
- Create: `backtest/evaluation.py`  (NOT `validation.py` — that name is taken by the go-live gate at repo root)
- Test: `tests/test_backtest_evaluation.py`

**Interfaces:**
- Consumes: `backtest.metrics.spearman_rank_ic`.
- Produces:
  - `def split_pairs(pairs: list[tuple[float, float]], frac: float = 0.5) -> tuple[list, list]` — deterministic chronological-agnostic split (first `frac` as discovery, rest as holdout); caller passes pairs already ordered by date.
  - `def best_horizon(ic_by_horizon: dict[int, float]) -> int` — horizon with max |IC| (discovery pick).
  - `def bonferroni_note(num_horizons: int, alpha: float = 0.05) -> str` — returns the caveat string `"Discovery over N horizons; treat any single p<alpha with Bonferroni alpha/N=…"`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_evaluation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.evaluation'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_evaluation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/evaluation.py tests/test_backtest_evaluation.py
git commit -m "feat(backtest): split-sample + multiple-comparison discipline"
```

---

### Task 5: Sentiment-source seam + fixture source

**Files:**
- Create: `backtest/sources.py`
- Test: `tests/test_backtest_sources.py`

**Interfaces:**
- Consumes: `backtest.series.SentimentSeries`, `load_sentiment_series`.
- Produces:
  - `class SentimentSource(Protocol)` with `name: str` and `def series(self, ticker: str) -> SentimentSeries`.
  - `class FixtureSentimentSource` — reads `<dir>/sentiment_<TICKER>.json` into a `SentimentSeries`; missing file → empty series. Mirrors `enrichment.providers.FixtureProvider`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_sources.py
import json

from backtest.sources import FixtureSentimentSource


def test_fixture_source_reads_series(tmp_path):
    (tmp_path / "sentiment_MU.json").write_text(
        json.dumps({"ticker": "MU", "points": [{"date": "2025-01-01", "value": 0.2}]}),
        encoding="utf-8",
    )
    src = FixtureSentimentSource(str(tmp_path))
    s = src.series("MU")
    assert s.ticker == "MU" and s.points[0].value == 0.2


def test_fixture_source_missing_is_empty(tmp_path):
    src = FixtureSentimentSource(str(tmp_path))
    assert src.series("NOPE").points == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_sources.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.sources'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/sources.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_sources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/sources.py tests/test_backtest_sources.py
git commit -m "feat(backtest): sentiment-source seam + fixture source"
```

---

### Task 6: Alignment + event-conditioned slice

**Files:**
- Create: `backtest/align.py`
- Test: `tests/test_backtest_align.py`

**Interfaces:**
- Consumes: `SentimentSeries`, `forward_returns` output (`dict[date, dict[horizon, float]]`).
- Produces:
  - `def align(sentiment: SentimentSeries, fwd: dict[str, dict[int, float]], horizon: int) -> list[tuple[float, float]]` — `(sentiment_value, forward_return)` pairs for dates present in BOTH, ordered by date. Optionally uses `level_or_delta`: see below.
  - `def to_delta(sentiment: SentimentSeries) -> SentimentSeries` — converts a level series to a day-over-day Δ series (first point dropped).
  - `def event_filtered(pairs_by_date: list[tuple[str, float, float]], event_dates: set[str], window: int) -> list[tuple[float, float]]` — keep only `(sent, ret)` whose date is within `window` days (positional, by the provided ordered date list) of an event date.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_align.py
from backtest.align import align, event_filtered, to_delta
from backtest.series import SentimentPoint, SentimentSeries


def test_align_intersects_dates():
    s = SentimentSeries("MU", [SentimentPoint("d1", 0.1), SentimentPoint("d2", 0.2)])
    fwd = {"d1": {5: 0.03}, "d3": {5: 0.09}}
    assert align(s, fwd, 5) == [(0.1, 0.03)]


def test_to_delta_first_difference():
    s = SentimentSeries("MU", [SentimentPoint("d1", 0.1), SentimentPoint("d2", 0.25)])
    d = to_delta(s)
    assert [round(p.value, 6) for p in d.points] == [0.15]
    assert d.points[0].date == "d2"


def test_event_filtered_keeps_only_event_window():
    rows = [("d1", 0.1, 0.01), ("d2", 0.2, 0.02), ("d3", 0.3, 0.03)]
    # event on d3, window 0 -> only d3 kept
    assert event_filtered(rows, {"d3"}, window=0) == [(0.3, 0.03)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_align.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.align'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
    dates = [d for d, _, _ in pairs_by_date]
    keep_idx: set[int] = set()
    for i, d in enumerate(dates):
        if d in event_dates:
            for j in range(max(0, i - window), min(len(dates), i + window + 1)):
                keep_idx.add(j)
    return [(s, r) for k, (_, s, r) in enumerate(pairs_by_date) if k in keep_idx]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_align.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/align.py tests/test_backtest_align.py
git commit -m "feat(backtest): alignment, level/delta, event-conditioned slice"
```

---

### Task 7: Runner — end-to-end on fixtures → IC/quantile/hit-rate report

**Files:**
- Create: `backtest/run.py`
- Test: `tests/test_backtest_run.py`

**Interfaces:**
- Consumes: every prior module.
- Produces:
  - `def run_backtest(sentiment_by_ticker: dict[str, SentimentSeries], prices_by_ticker: dict[str, PriceSeries], horizons: list[int], *, mode: str = "level", q: int = 5) -> dict` — pools `(sentiment, fwd_return)` pairs across all tickers per horizon, then returns `{"horizons": {h: {"ic": float, "quantiles": list[float], "hit_rate": float, "n": int}}, "best_horizon": int, "caveat": str}`. `mode` ∈ `{"level","delta"}`.
  - `def report_markdown(result: dict) -> str` — renders a Bigdata.com-branded markdown table + the go/no-go caveat.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_run.py
from backtest.run import report_markdown, run_backtest
from backtest.series import PriceSeries, SentimentPoint, SentimentSeries


def _ramp_prices(ticker, n):
    return PriceSeries(ticker, {f"2025-{m:02d}-01": 100.0 * (1.05 ** m) for m in range(1, n + 1)})


def test_run_backtest_positive_ic_when_sentiment_leads_returns():
    # sentiment rises with the date index; prices ramp up -> positive IC at h=1
    s = SentimentSeries("X", [SentimentPoint(f"2025-{m:02d}-01", float(m)) for m in range(1, 8)])
    res = run_backtest({"X": s}, {"X": _ramp_prices("X", 8)}, [1], mode="level")
    assert res["horizons"][1]["n"] >= 5
    assert res["horizons"][1]["ic"] > 0
    assert res["best_horizon"] == 1
    md = report_markdown(res)
    assert "Bigdata.com" in md and "IC" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_run.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.run'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/run.py
"""End-to-end backtest runner: pool pairs across tickers, sweep horizons, emit
metrics + a go/no-go report. Offline; fed by any SentimentSource."""

from backtest.align import align, to_delta
from backtest.evaluation import best_horizon, bonferroni_note
from backtest.metrics import hit_rate, quantile_returns, spearman_rank_ic
from backtest.returns import forward_returns
from backtest.series import PriceSeries, SentimentSeries


def run_backtest(
    sentiment_by_ticker: dict[str, SentimentSeries],
    prices_by_ticker: dict[str, PriceSeries],
    horizons: list[int],
    *,
    mode: str = "level",
    q: int = 5,
) -> dict:
    horizons_out: dict[int, dict] = {}
    for h in horizons:
        pooled: list[tuple[float, float]] = []
        for tkr, s in sentiment_by_ticker.items():
            if tkr not in prices_by_ticker:
                continue
            series = to_delta(s) if mode == "delta" else s
            fwd = forward_returns(prices_by_ticker[tkr], [h])
            pooled.extend(align(series, fwd, h))
        horizons_out[h] = {
            "ic": spearman_rank_ic(pooled),
            "quantiles": quantile_returns(pooled, q=q),
            "hit_rate": hit_rate(pooled),
            "n": len(pooled),
        }
    ic_by_h = {h: horizons_out[h]["ic"] for h in horizons}
    return {
        "horizons": horizons_out,
        "best_horizon": best_horizon(ic_by_h) if ic_by_h else None,
        "caveat": bonferroni_note(len(horizons)),
        "mode": mode,
    }


def report_markdown(result: dict) -> str:
    lines = [
        "## Bigdata.com sentiment backtest — go/no-go (https://bigdata.com)",
        f"Mode: **{result['mode']}** · best horizon (discovery): "
        f"**{result['best_horizon']}d**",
        "",
        "| Horizon (d) | n | Rank IC | Hit rate | Quantile fwd returns (low→high) |",
        "|---|---|---|---|---|",
    ]
    for h, m in sorted(result["horizons"].items()):
        qs = ", ".join(f"{x:.4f}" for x in m["quantiles"])
        lines.append(
            f"| {h} | {m['n']} | {m['ic']:.4f} | {m['hit_rate']:.2%} | {qs} |"
        )
    lines += ["", f"> {result['caveat']}"]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_run.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full gate + commit**

Run: `ruff check . ; ruff format --check . ; python -m pytest -q`
Expected: all green.

```bash
git add backtest/run.py tests/test_backtest_run.py
git commit -m "feat(backtest): end-to-end runner + go/no-go report"
```

---

### Task 8: Interim MCP-approx pilot (LLM-scored sentiment series) — operator-run

**Files:**
- Create: `backtest/scorer_llm.py`
- Create: `backtest/pilot/README.md`
- Create: `backtest/pilot/capture_windows.md` (operator runbook for the MCP capture)
- Test: `tests/test_backtest_scorer_llm.py` (offline — tests the prompt builder + response parser only, no live call)

**Interfaces:**
- Produces:
  - `def build_scoring_prompt(ticker: str, window_label: str, headlines: list[str]) -> str` — builds the Claude prompt asking for a single media-tone sentiment score in `[-1, 1]`.
  - `def parse_score(text: str) -> float` — extracts the float the model returns (expects a bare number or `SCORE: x`), clamps to `[-1, 1]`; raises `ValueError` on no parseable number.
  - `def score_window(client, ticker, window_label, headlines) -> float` — calls the existing Anthropic client and returns the parsed score. (Live; not in CI.)

**Approximation caveat (document in README):** this substitutes Claude's tone scoring for RavenPack's sentiment, so results are DIRECTIONAL ONLY — they test whether *a* sentiment signal predicts returns, not whether *Bigdata.com's* does. The faithful run is Task 9.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_scorer_llm.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_scorer_llm.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.scorer_llm'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/scorer_llm.py
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
        messages=[{"role": "user", "content": build_scoring_prompt(ticker, window_label, headlines)}],
    )
    return parse_score(resp.content[0].text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_scorer_llm.py -q`
Expected: PASS.

- [ ] **Step 5: Write the operator runbooks, run the scoped pilot, commit**

Write `backtest/pilot/capture_windows.md` documenting the MCP capture loop (operator runs these in a Claude session with the Bigdata.com connector):
- For each of ~50–100 liquid names × monthly windows over ~2–3y: call `bigdata_search` (fast mode, `entity` filter = resolved `rp_entity_id`, `timestamp` = month window, `max_chunks` 12). Save each window's `headline` list to `backtest/pilot/raw/<TICKER>/<YYYY-MM>.json`. Budget note: ~1.2 query units/window (probe-measured) → 100 names × 30 months ≈ 3,000 windows ≈ ~3,600 units; **start with 5–10 names to confirm the wiring and cost before fanning out**.

Write `backtest/pilot/README.md`: how to (1) run `score_window` over the captured `raw/` tree to emit `sentiment_<TICKER>.json` (Task 5 fixture shape), (2) `fetch_price_series` per name (Task 2b), (3) call `run_backtest` + `report_markdown`, (4) read the result as DIRECTIONAL-ONLY pending Task 9.

Run the small (5–10 name) pilot, capture the report into `backtest/pilot/RESULT-approx-<date>.md`.

```bash
git add backtest/scorer_llm.py backtest/pilot/ tests/test_backtest_scorer_llm.py
git commit -m "feat(backtest): interim MCP-approx LLM scorer + pilot runbook"
```

---

### Task 9: Faithful REST sentiment source (DEFERRED — gated on the business-email REST key)

**Files:**
- Create: `backtest/sources_rest.py`
- Create: `docs/superpowers/notes/bigdata-rest-sentiment-discovery.md`
- Test: `tests/test_backtest_sources_rest.py` (offline — tests the JSON→SentimentSeries parser against a RECORDED fixture captured during discovery)

**Blocked-by:** business-email `BIGDATA_API_KEY` (memory `bigdata-evaluation-and-trading-split` — custom-domain email route). Do NOT start until the key exists; the probe (2026-06-20) proved the MCP path cannot supply a faithful historical entity-sentiment series.

**Interfaces:**
- Produces: `class RestSentimentSource` (implements `backtest.sources.SentimentSource`) — fetches RavenPack's historical sentiment dataset for an `rp_entity_id` over a date range and returns a `SentimentSeries`.

- [ ] **Step 1: Discovery (no code).** With the key live, read docs.bigdata.com and confirm whether a purpose-built **historical sentiment / RavenPack Analytics time-series** endpoint exists (entity-resolved daily/period sentiment). Capture ONE raw response for a known entity (e.g. MU=49BBBC) to `docs/superpowers/notes/bigdata-rest-sentiment-discovery.md` and record the exact JSON field paths (date field, sentiment value field). This mirrors the field-name verification owed for `providers_bigdata.py` (memory `bigdata-enrichment-validated-live`, gap #1).

- [ ] **Step 2: Write the failing test** against a small recorded fixture of that raw response shape.

```python
# tests/test_backtest_sources_rest.py
from backtest.sources_rest import parse_rest_sentiment


def test_parse_rest_sentiment_to_series():
    # SHAPE PLACEHOLDER until Step 1 records the real field paths — update both
    # this fixture and parse_rest_sentiment to the confirmed field names.
    raw = {"entity_id": "49BBBC", "series": [{"date": "2025-01-31", "sentiment": 0.12}]}
    s = parse_rest_sentiment("MU", raw)
    assert s.points[0].date == "2025-01-31" and s.points[0].value == 0.12
```

- [ ] **Step 3: Run test to verify it fails / Step 4: implement `parse_rest_sentiment` + `RestSentimentSource` against the CONFIRMED field names from Step 1 / Step 5: full gate + commit.** (Code intentionally not pre-written here — it must match the real REST shape discovered in Step 1, not a guess. The shipped REST client `enrichment/providers_bigdata.py` `_post` is the auth/HTTP reference to reuse.)

- [ ] **Step 6: Faithful run.** Swap `FixtureSentimentSource`→`RestSentimentSource` in the pilot harness, run the full broad-universe sweep, produce `backtest/pilot/RESULT-faithful-<date>.md` with the IC/quantile/hit-rate tables, event-conditioned slice, held-out confirmation, and the explicit **go/no-go on sizing**.

---

## Self-Review

**Spec coverage:** hypothesis (rank IC + quantile + hit rate) → Tasks 3,7; event-conditioned slice → Task 6; horizon sweep 1d–3mo → Task 7 (`horizons` list, e.g. `[1,5,21,63]`); discovery/confirmation discipline → Task 4; broad-universe pilot-first → Task 8 (small-first) → Task 9 (full); data-path feasibility risk → retired by the 2026-06-20 probe, reflected in the Task 8/9 split; "never auto-sizes" → Global Constraints; graceful degradation/vendor seam → Task 5 (`SentimentSource`); deliverable = go/no-go report → Tasks 7 (`report_markdown`) + 9 Step 6.

**Deviations from spec (intentional, evidence-driven):** (1) historical series is NOT reconstructed from `bigdata_search` (probe showed no numeric sentiment surfaced) — faithful series comes from REST analytics (Task 9), interim is LLM-scored (Task 8). (2) `backtest/` is offline-only → no Dockerfile/CI-path change (contrast `enrichment/`).

**Placeholder scan:** the only deliberate placeholder is Task 9's parser shape, explicitly gated on Step-1 discovery (writing concrete code against an unknown REST schema would be a guess — the skill's lesser evil is a discovery-first task). All Tasks 1–8 carry complete, runnable code.

**Type consistency:** `SentimentSeries`/`PriceSeries`/`SentimentPoint` names and the `(sentiment, fwd_return)` pair tuple are used consistently across Tasks 1–9; `forward_returns` returns `dict[date, dict[horizon, float]]` consumed unchanged by `align` and `run_backtest`.
