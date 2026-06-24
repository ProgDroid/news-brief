# Alpha Vantage Sentiment Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline, self-serve Alpha Vantage sentiment backtest (two factors: weekly news-tone and per-call transcript sentiment) that reuses the existing `backtest/` engine to produce held-out IC / quantile / hit-rate reports — the conclusive go/no-go the n≈18 pilot could not give.

**Architecture:** New `backtest/avpull/` package = a resumable AV puller (operator-run) + pure transforms that emit the engine's `SentimentSeries` JSON shape; the existing engine (`run_backtest`, `report_markdown`, `FixtureSentimentSource`, `prices_yf`) runs unchanged except a one-line source-label parameterization in `report_markdown`. Pure transforms are TDD'd in CI; network/yfinance code is operator-run and excluded from CI (mirrors `prices_yf` / `scorer_llm`).

**Tech Stack:** Python 3, stdlib only for CI-tested code (`urllib`, `datetime`, `statistics`, `bisect`, `json`); `yfinance`/`pandas` only in operator-run paths; `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-06-24-av-sentiment-backtest-design.md`

## Global Constraints

- **New code is offline-only.** Nothing in `backtest/avpull/` is imported by the cron app, the Dockerfile, or CI runtime. No new CI dependencies.
- **CI has no pandas/yfinance.** Any test needing them must `pd = pytest.importorskip("pandas")`. The transforms and puller pure-helpers are pure stdlib — they run in CI without guards.
- **AV JSON numbers are strings.** `ticker_sentiment_score`, `relevance_score`, segment `sentiment` arrive as strings — always `float()` them.
- **`report_markdown` default output must not change.** Existing `tests/test_backtest_run.py` asserts `"Bigdata.com" in md`; the new `source_label`/`source_url` params default to `"Bigdata.com"`/`"https://bigdata.com"`.
- **Test files are flat:** `tests/test_backtest_<module>.py`.
- **Commits via the Bash tool** (PowerShell prepends a BOM to commit subjects). Messages containing backticks/`$`/`!` use `git commit -F -` with a single-quoted heredoc.
- **Pre-push gate:** `ruff check .` + `ruff format --check .` + `pytest`; stage every file ruff reformats or CI fails.
- **Commit straight to `main`** (solo repo; do not branch).
- **Pull strategy:** one month of AV premium (75 req/min); puller paces ≤75/min and is resumable via a manifest.

---

### Task 1: avpull package + universe

**Files:**
- Create: `backtest/avpull/__init__.py` (empty)
- Create: `backtest/avpull/universe.py`
- Test: `tests/test_backtest_avpull.py`

**Interfaces:**
- Produces: `UNIVERSE: list[str]` — ~37 de-duplicated tickers (yfinance symbols == AV symbols for these names).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_avpull.py
from backtest.avpull.universe import UNIVERSE


def test_universe_is_deduped_and_covers_watchlist():
    assert len(UNIVERSE) == len(set(UNIVERSE))  # no dupes
    assert 30 <= len(UNIVERSE) <= 45  # diversified, not the n=18 pilot
    for w in ("CVX", "MU", "RGLD", "ESLT", "AVAV"):  # watchlist single-stocks
        assert w in UNIVERSE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_avpull.py::test_universe_is_deduped_and_covers_watchlist -v`
Expected: FAIL with `ModuleNotFoundError: backtest.avpull`

- [ ] **Step 3: Create the package + universe**

```python
# backtest/avpull/__init__.py
```
(empty file)

```python
# backtest/avpull/universe.py
"""Backtest universe: ~37 diversified large-caps (watchlist single-stocks +
sector spread) to break the pilot's all-trending-up confound. yfinance and AV
use the same symbol for every name here. See spec §6 (survivorship-bias caveat)."""

UNIVERSE: list[str] = [
    # watchlist single-stocks
    "CVX", "MU", "RGLD", "ESLT", "AVAV",
    # tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMD", "CRM",
    # financials
    "JPM", "BAC", "GS", "V",
    # healthcare
    "JNJ", "UNH", "PFE", "LLY",
    # energy
    "XOM", "COP",
    # consumer
    "AMZN", "WMT", "KO", "PG", "MCD", "NKE",
    # industrials
    "CAT", "BA", "HON", "GE", "LMT",
    # comms
    "META", "DIS", "NFLX",
    # materials
    "NEM", "FCX",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest/avpull/__init__.py backtest/avpull/universe.py tests/test_backtest_avpull.py
git commit -m "feat(backtest): AV backtest universe (~37 diversified large-caps)"
```

---

### Task 2: News-tone transform

**Files:**
- Create: `backtest/avpull/transforms.py`
- Test: `tests/test_backtest_avpull.py` (append)

**Interfaces:**
- Produces: `news_series_from_pages(ticker: str, pages: list[dict], *, start_date: str = "2018-01-01") -> dict` returning `{"ticker": str, "points": [{"date": "YYYY-MM-DD", "value": float}, ...]}` (engine `SentimentSeries` JSON shape). Relevance-weighted weekly mean, points dated to each ISO week's Friday, articles before `start_date` dropped.
- Produces (internal, exported for testing): `_iso_week_friday(d: datetime.date) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_avpull.py  (append)
from datetime import date

from backtest.avpull.transforms import _iso_week_friday, news_series_from_pages


def test_iso_week_friday_maps_any_weekday_into_same_iso_week():
    assert _iso_week_friday(date(2024, 1, 15)) == "2024-01-19"  # Mon -> that Fri
    assert _iso_week_friday(date(2024, 1, 19)) == "2024-01-19"  # Fri -> itself
    assert _iso_week_friday(date(2024, 1, 21)) == "2024-01-19"  # Sun -> that Fri


def _news_item(tp, ticker, rel, score):
    return {
        "time_published": tp,
        "ticker_sentiment": [
            {"ticker": ticker, "relevance_score": str(rel),
             "ticker_sentiment_score": str(score)}
        ],
    }


def test_news_series_relevance_weighted_weekly_mean():
    # two articles same ISO week: weights 0.2 and 0.8 -> weighted mean of scores
    page = {"feed": [
        _news_item("20240115T120000", "MU", 0.2, 1.0),
        _news_item("20240117T120000", "MU", 0.8, 0.0),
    ]}
    out = news_series_from_pages("MU", [page])
    assert out["ticker"] == "MU"
    assert out["points"] == [{"date": "2024-01-19", "value": 0.2}]  # (0.2*1+0.8*0)/1.0


def test_news_series_drops_pre_2018_and_other_tickers():
    page = {"feed": [
        _news_item("20150101T120000", "MU", 1.0, 0.5),   # pre-2018 -> dropped
        _news_item("20240115T120000", "AAPL", 1.0, 0.9),  # wrong ticker -> ignored
        _news_item("20240115T120000", "MU", 1.0, 0.3),
    ]}
    out = news_series_from_pages("MU", [page])
    assert out["points"] == [{"date": "2024-01-19", "value": 0.3}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: FAIL with `ImportError: cannot import name 'news_series_from_pages'`

- [ ] **Step 3: Implement**

```python
# backtest/avpull/transforms.py
"""Pure transforms: raw Alpha Vantage JSON -> engine SentimentSeries dicts.
No network. AV numeric fields arrive as strings and are coerced to float."""

from datetime import date, timedelta
from statistics import mean


def _iso_week_friday(d: date) -> str:
    """Friday (ISO weekday 5) of d's ISO week, as YYYY-MM-DD. weekday(): Mon=0..
    Sun=6, so 4 - weekday lands on that week's Friday for every day Mon–Sun."""
    return (d + timedelta(days=4 - d.weekday())).isoformat()


def _news_date(time_published: str) -> date:
    # AV format: 'YYYYMMDDTHHMMSS'
    return date(
        int(time_published[0:4]), int(time_published[4:6]), int(time_published[6:8])
    )


def news_series_from_pages(
    ticker: str, pages: list[dict], *, start_date: str = "2018-01-01"
) -> dict:
    start = date.fromisoformat(start_date)
    tkr = ticker.upper()
    num: dict[str, float] = {}  # friday -> sum(rel*score)
    den: dict[str, float] = {}  # friday -> sum(rel)
    for page in pages:
        for item in page.get("feed", []):
            d = _news_date(item["time_published"])
            if d < start:
                continue
            for ts in item.get("ticker_sentiment", []):
                if (ts.get("ticker") or "").upper() != tkr:
                    continue
                rel = float(ts["relevance_score"])
                score = float(ts["ticker_sentiment_score"])
                wk = _iso_week_friday(d)
                num[wk] = num.get(wk, 0.0) + rel * score
                den[wk] = den.get(wk, 0.0) + rel
    points = [
        {"date": wk, "value": num[wk] / den[wk]} for wk in sorted(num) if den[wk] > 0
    ]
    return {"ticker": ticker, "points": points}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: PASS (all news + universe tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/avpull/transforms.py tests/test_backtest_avpull.py
git commit -m "feat(backtest): AV news-tone transform (relevance-weighted weekly mean)"
```

---

### Task 3: Transcript transform

**Files:**
- Modify: `backtest/avpull/transforms.py` (append functions)
- Test: `tests/test_backtest_avpull.py` (append)

**Interfaces:**
- Produces: `transcript_series_from_calls(ticker: str, calls: list[dict]) -> dict` — one point per call = mean segment `sentiment` over non-boilerplate segments, dated via `quarter_to_anchor_date`. Calls with no usable segment are skipped.
- Produces: `quarter_to_anchor_date(quarter: str, *, offset_days: int = 50) -> str` — `'YYYYQM'` → that calendar quarter's end + `offset_days`, ISO date (lookahead-safe anchor; see spec §10.3).
- Produces (internal): `_is_boilerplate(seg: dict) -> bool` — True for Operator turns and Investor-Relations intro turns.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_avpull.py  (append)
from backtest.avpull.transforms import (
    quarter_to_anchor_date,
    transcript_series_from_calls,
)


def test_quarter_to_anchor_date_is_quarter_end_plus_offset():
    # 2024Q1 calendar end = 2024-03-31; +50d = 2024-05-20
    assert quarter_to_anchor_date("2024Q1") == "2024-05-20"
    # 2023Q4 end = 2023-12-31; +50d = 2024-02-19
    assert quarter_to_anchor_date("2023Q4") == "2024-02-19"


def test_transcript_series_means_non_boilerplate_segments():
    call = {"symbol": "MU", "quarter": "2024Q1", "transcript": [
        {"speaker": "Operator", "title": "", "sentiment": "0.0"},        # excluded
        {"speaker": "Satya Kumar", "title": "Investor Relations", "sentiment": "0.0"},  # excluded
        {"speaker": "Sanjay Mehrotra", "title": "President and CEO", "sentiment": "0.8"},
        {"speaker": "Mark Murphy", "title": "CFO", "sentiment": "0.4"},
    ]}
    out = transcript_series_from_calls("MU", [call])
    assert out["ticker"] == "MU"
    assert out["points"] == [{"date": "2024-05-20", "value": 0.6}]  # mean(0.8, 0.4)


def test_transcript_series_skips_calls_with_no_usable_segments():
    call = {"symbol": "MU", "quarter": "2024Q1", "transcript": [
        {"speaker": "Operator", "title": "", "sentiment": "0.0"},
    ]}
    assert transcript_series_from_calls("MU", [call])["points"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: FAIL with `ImportError: cannot import name 'transcript_series_from_calls'`

- [ ] **Step 3: Implement (append to transforms.py)**

```python
def _is_boilerplate(seg: dict) -> bool:
    speaker = (seg.get("speaker") or "").strip().lower()
    title = (seg.get("title") or "").strip().lower()
    return speaker == "operator" or "investor relations" in title


def quarter_to_anchor_date(quarter: str, *, offset_days: int = 50) -> str:
    year = int(quarter[:4])
    q = int(quarter[-1])
    end_month = q * 3
    if end_month == 12:
        qend = date(year, 12, 31)
    else:
        qend = date(year, end_month + 1, 1) - timedelta(days=1)
    return (qend + timedelta(days=offset_days)).isoformat()


def transcript_series_from_calls(ticker: str, calls: list[dict]) -> dict:
    points = []
    for call in calls:
        segs = [
            float(s["sentiment"])
            for s in call.get("transcript", [])
            if not _is_boilerplate(s)
        ]
        if not segs:
            continue
        points.append(
            {"date": quarter_to_anchor_date(call["quarter"]), "value": mean(segs)}
        )
    points.sort(key=lambda p: p["date"])
    return {"ticker": ticker, "points": points}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest/avpull/transforms.py tests/test_backtest_avpull.py
git commit -m "feat(backtest): AV transcript transform (per-call non-boilerplate mean)"
```

---

### Task 4: Calendar snap

**Files:**
- Modify: `backtest/avpull/transforms.py` (append)
- Test: `tests/test_backtest_avpull.py` (append)

**Interfaces:**
- Produces: `snap_series_to_calendar(series_dict: dict, trading_days: set[str]) -> dict` — snaps each point's date to the latest trading day ≤ its date (no lookahead); drops points with no trading day within 10 calendar days before; on collision the later point in the input wins.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_avpull.py  (append)
from backtest.avpull.transforms import snap_series_to_calendar


def test_snap_maps_to_latest_trading_day_not_after():
    series = {"ticker": "X", "points": [{"date": "2024-05-20", "value": 0.6}]}  # Mon
    cal = {"2024-05-17", "2024-05-21"}  # Fri before, Tue after
    out = snap_series_to_calendar(series, cal)
    assert out["points"] == [{"date": "2024-05-17", "value": 0.6}]  # nearest <=, no lookahead


def test_snap_drops_points_with_no_nearby_trading_day():
    series = {"ticker": "X", "points": [{"date": "2024-05-20", "value": 0.6}]}
    cal = {"2024-01-02"}  # months away
    assert snap_series_to_calendar(series, cal)["points"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: FAIL with `ImportError: cannot import name 'snap_series_to_calendar'`

- [ ] **Step 3: Implement (append to transforms.py)**

```python
import bisect  # add to the top-of-file imports


def snap_series_to_calendar(series_dict: dict, trading_days: set[str]) -> dict:
    cal = sorted(trading_days)
    out: dict[str, float] = {}
    for p in series_dict.get("points", []):
        i = bisect.bisect_right(cal, p["date"]) - 1
        if i < 0:
            continue
        snapped = cal[i]
        gap = (date.fromisoformat(p["date"]) - date.fromisoformat(snapped)).days
        if gap > 10:
            continue
        out[snapped] = p["value"]  # later input point wins a collision
    return {
        "ticker": series_dict["ticker"],
        "points": [{"date": d, "value": v} for d, v in sorted(out.items())],
    }
```

(Move `import bisect` to the module's import block alongside `from datetime import ...`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest/avpull/transforms.py tests/test_backtest_avpull.py
git commit -m "feat(backtest): snap AV sentiment points to the price calendar"
```

---

### Task 5: Puller pure helpers

**Files:**
- Create: `backtest/avpull/pull.py`
- Test: `tests/test_backtest_avpull.py` (append)

**Interfaces:**
- Produces: `is_throttled(resp: dict) -> bool` — True if AV returned an `Information`/`Note`/`Error Message` body instead of data.
- Produces: `quarters_2010_to(end_year: int, end_q: int) -> list[str]` — `['2010Q1', …, '<end_year>Q<end_q>']` inclusive.
- Produces: `pending_units(universe: list[str], quarters: list[str], done: set[tuple]) -> list[tuple]` — every `("news", t)` and `("transcript", t, q)` not in `done`, stable order.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_avpull.py  (append)
from backtest.avpull.pull import is_throttled, pending_units, quarters_2010_to


def test_is_throttled_detects_av_notices():
    assert is_throttled({"Information": "rate limit ..."}) is True
    assert is_throttled({"Note": "..."}) is True
    assert is_throttled({"feed": []}) is False


def test_quarters_2010_to_is_inclusive():
    qs = quarters_2010_to(2010, 3)
    assert qs == ["2010Q1", "2010Q2", "2010Q3"]
    assert quarters_2010_to(2011, 1)[-2:] == ["2010Q4", "2011Q1"]


def test_pending_units_excludes_done_keeps_order():
    done = {("news", "MU")}
    units = pending_units(["MU", "CVX"], ["2010Q1"], done)
    assert ("news", "MU") not in units
    assert units[0] == ("news", "CVX")
    assert ("transcript", "MU", "2010Q1") in units
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: FAIL with `ModuleNotFoundError: backtest.avpull.pull`

- [ ] **Step 3: Implement the pure helpers**

```python
# backtest/avpull/pull.py
"""Resumable Alpha Vantage puller. Pure helpers (CI-tested) + live fetch/run
(operator-run, network — NOT in CI, like prices_yf / scorer_llm)."""


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_backtest_avpull.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest/avpull/pull.py tests/test_backtest_avpull.py
git commit -m "feat(backtest): AV puller pure helpers (throttle/quarters/pending)"
```

---

### Task 6: Parameterize `report_markdown` source label

**Files:**
- Modify: `backtest/run.py:84-128` (`report_markdown`)
- Test: `tests/test_backtest_run.py` (append)

**Interfaces:**
- Modifies: `report_markdown(result: dict, *, source_label: str = "Bigdata.com", source_url: str = "https://bigdata.com") -> str` — header now reads `## {source_label} sentiment backtest … ({source_url})`. Default output is byte-identical to today (existing tests rely on `"Bigdata.com"`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_run.py  (append)
def test_report_markdown_accepts_a_source_label():
    s, prices = _monotonic(10)
    res = run_backtest({"X": s}, {"X": prices}, [1])
    md = report_markdown(res, source_label="Alpha Vantage",
                         source_url="https://www.alphavantage.co")
    assert "Alpha Vantage" in md
    assert "https://www.alphavantage.co" in md
    assert "Bigdata.com" not in md.splitlines()[0]  # header switched cleanly
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backtest_run.py::test_report_markdown_accepts_a_source_label -v`
Expected: FAIL (header hardcodes "Bigdata.com")

- [ ] **Step 3: Edit `report_markdown`**

Change the signature and the first header line in `backtest/run.py`:

```python
def report_markdown(
    result: dict,
    *,
    source_label: str = "Bigdata.com",
    source_url: str = "https://bigdata.com",
) -> str:
    best = result["best_horizon"]
    frac = result["split_frac"]
    sentiment_scale = (
        "per-ticker standardized" if result.get("standardize") else "raw levels"
    )
    lines = [
        f"## {source_label} sentiment backtest — discovery + held-out "
        f"confirmation ({source_url})",
        f"> {_DISCOVERY_CAVEAT}",
        "",
        # ... rest unchanged ...
```

(Only the signature and that first `lines[0]` entry change; the rest of the function body is untouched.)

- [ ] **Step 4: Run to verify the new test passes AND existing ones stay green**

Run: `python -m pytest tests/test_backtest_run.py -v`
Expected: PASS — including `test_run_backtest_positive_ic_in_discovery_and_holdout` and `test_report_handles_empty_holdout_gracefully` (they use the default label, so `"Bigdata.com"` still appears).

- [ ] **Step 5: Commit**

```bash
git add backtest/run.py tests/test_backtest_run.py
git commit -m "refactor(backtest): parameterize report_markdown source label"
```

---

### Task 7: Live AV client + resumable puller (operator-run)

**Files:**
- Modify: `backtest/avpull/pull.py` (append live functions)

**Interfaces:**
- Produces: `fetch_news_pages(ticker, api_key, *, time_from="20180101T0000", limit=1000) -> list[dict]`
- Produces: `fetch_transcript(symbol, quarter, api_key) -> dict`
- Produces: `run(api_key, out_dir, *, end_year, end_q, per_min=75) -> None` — pulls all `pending_units`, writing raw JSON to `<out_dir>/raw/` and updating `<out_dir>/manifest.json` after each unit (resumable).

> Network code — **not** unit-tested in CI (no key, no network), mirroring `prices_yf.fetch_price_series` / `scorer_llm.score_window`. Deliverable is a manual smoke run.

- [ ] **Step 1: Append the live functions to `pull.py`**

```python
import json  # add to pull.py imports
import time
import urllib.parse
import urllib.request
from pathlib import Path

_BASE = "https://www.alphavantage.co/query"


def _get(params: dict) -> dict:
    url = _BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (fixed host)
        return json.loads(r.read().decode())


def fetch_news_pages(
    ticker: str, api_key: str, *, time_from: str = "20180101T0000", limit: int = 1000
) -> list[dict]:
    """Page NEWS_SENTIMENT forward by time until a short/empty page. Raises on
    throttle so the caller's manifest does not mark the unit done."""
    pages: list[dict] = []
    cursor, prev_last = time_from, None
    while True:
        resp = _get({
            "function": "NEWS_SENTIMENT", "tickers": ticker, "time_from": cursor,
            "sort": "EARLIEST", "limit": str(limit), "apikey": api_key,
        })
        if is_throttled(resp):
            raise RuntimeError(f"AV throttled on news {ticker}: {resp}")
        feed = resp.get("feed", [])
        if not feed:
            break
        pages.append(resp)
        last_tp = feed[-1]["time_published"]
        if len(feed) < limit or last_tp == prev_last:
            break
        prev_last, cursor = last_tp, last_tp
        time.sleep(0.8)
    return pages


def fetch_transcript(symbol: str, quarter: str, api_key: str) -> dict:
    resp = _get({
        "function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol,
        "quarter": quarter, "apikey": api_key,
    })
    if is_throttled(resp):
        raise RuntimeError(f"AV throttled on transcript {symbol} {quarter}: {resp}")
    return resp


def run(api_key: str, out_dir: str, *, end_year: int, end_q: int, per_min: int = 75) -> None:
    from backtest.avpull.universe import UNIVERSE

    out = Path(out_dir)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    man = out / "manifest.json"
    done: set[tuple] = (
        {tuple(u) for u in json.loads(man.read_text())} if man.exists() else set()
    )
    quarters = quarters_2010_to(end_year, end_q)
    delay = 60.0 / per_min
    for unit in pending_units(UNIVERSE, quarters, done):
        if unit[0] == "news":
            _, t = unit
            (raw / f"news_{t}.json").write_text(json.dumps(fetch_news_pages(t, api_key)))
        else:
            _, t, q = unit
            (raw / f"tx_{t}_{q}.json").write_text(
                json.dumps(fetch_transcript(t, q, api_key))
            )
        done.add(unit)
        man.write_text(json.dumps([list(u) for u in done]))
        time.sleep(delay)
    print(f"pull complete: {len(done)} units in {out}")
```

- [ ] **Step 2: Manual smoke (operator, on AV premium)**

Set the key and pull a tiny slice first to confirm shapes, then the full universe:

```bash
# scratchpad smoke — ONE name, current quarter only, to verify wiring
python -c "from backtest.avpull import pull; pull.run('YOUR_PREMIUM_KEY', './scratch_av', end_year=2026, end_q=1, per_min=75)"
```
Expected: `./scratch_av/raw/` fills with `news_<T>.json` + `tx_<T>_<Q>.json`, `manifest.json` grows, and a final `pull complete: N units` line. Re-running resumes (already-done units skipped). Confirm a `tx_*.json` has a top-level `transcript` list and (per spec §10.3) note whether any call-date field exists.

- [ ] **Step 3: Lint + commit (no CI test for network code)**

```bash
ruff check backtest/avpull/pull.py && ruff format --check backtest/avpull/pull.py
git add backtest/avpull/pull.py
git commit -m "feat(backtest): live AV client + resumable puller (operator-run)"
```

---

### Task 8: Backtest runner wiring (operator-run)

**Files:**
- Create: `backtest/avpull/run_av_backtest.py`

**Interfaces:**
- Consumes: `news_series_from_pages`, `transcript_series_from_calls`, `snap_series_to_calendar` (Tasks 2–4); `UNIVERSE` (Task 1); `prices_yf.fetch_price_series`; `run_backtest`, `report_markdown` (Task 6); `series.load_sentiment_series`.
- Produces: `main(cache_dir: str, out_dir: str, *, start="2017-06-01", end="2026-06-30") -> None` — builds both factor series, snaps to each ticker's price calendar, runs the engine per factor (standardized), writes `RESULT-av-news.md` and `RESULT-av-transcript.md`.

> yfinance + wiring — operator-run, **not** in CI. Deliverable is the two reports.

- [ ] **Step 1: Create the runner**

```python
# backtest/avpull/run_av_backtest.py
"""Operator-run: cached AV raw JSON -> two factor series -> existing engine ->
two held-out reports. yfinance for prices; NOT imported by the cron app / CI."""

import json
from pathlib import Path

from backtest.avpull.transforms import (
    news_series_from_pages,
    snap_series_to_calendar,
    transcript_series_from_calls,
)
from backtest.avpull.universe import UNIVERSE
from backtest.prices_yf import fetch_price_series
from backtest.run import report_markdown, run_backtest
from backtest.series import load_sentiment_series

HORIZONS = [1, 5, 10, 21, 63]
_AV = dict(source_label="Alpha Vantage", source_url="https://www.alphavantage.co")


def _news_series(raw: Path, t: str) -> dict:
    p = raw / f"news_{t}.json"
    pages = json.loads(p.read_text()) if p.exists() else []
    return news_series_from_pages(t, pages)


def _transcript_series(raw: Path, t: str) -> dict:
    calls = []
    for p in sorted(raw.glob(f"tx_{t}_*.json")):
        resp = json.loads(p.read_text())
        if resp.get("transcript"):
            calls.append(resp)
    return transcript_series_from_calls(t, calls)


def main(cache_dir: str, out_dir: str, *, start: str = "2017-06-01",
         end: str = "2026-06-30") -> None:
    raw = Path(cache_dir) / "raw"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    prices, cals = {}, {}
    for t in UNIVERSE:
        ps = fetch_price_series(t, start, end)
        prices[t] = ps
        cals[t] = set(ps.closes.keys())

    for factor, loader in (("news", _news_series), ("transcript", _transcript_series)):
        sentiment = {}
        for t in UNIVERSE:
            snapped = snap_series_to_calendar(loader(raw, t), cals[t])
            sentiment[t] = load_sentiment_series(snapped)
        res = run_backtest(sentiment, prices, HORIZONS, mode="level", standardize=True)
        (out / f"RESULT-av-{factor}.md").write_text(report_markdown(res, **_AV))
        conf = res["confirmation"]
        print(f"{factor}: best={res['best_horizon']}d  held-out={conf}")
```

- [ ] **Step 2: Manual run (after Task 7's full pull completes)**

```bash
python -c "from backtest.avpull import run_av_backtest as r; r.main('./scratch_av', './scratch_av/results')"
```
Expected: `./scratch_av/results/RESULT-av-news.md` and `RESULT-av-transcript.md` written; two `best=… held-out=…` lines printed. Read the held-out IC / quantile / hit-rate in each report.

- [ ] **Step 3: Lint + commit**

```bash
ruff check backtest/avpull/run_av_backtest.py && ruff format --check backtest/avpull/run_av_backtest.py
git add backtest/avpull/run_av_backtest.py
git commit -m "feat(backtest): AV backtest runner wiring (two factor reports)"
```

---

### Task 9: Operator runbook

**Files:**
- Create: `backtest/avpull/README.md`

- [ ] **Step 1: Write the runbook**

````markdown
# AV sentiment backtest — operator runbook

Offline research tool (NOT in the cron image / CI). Needs Alpha Vantage
**premium** for the bulk pull (free tier = ~95 days; premium 75 req/min = ~35 min).

## 1. Pull (one premium month, ~35 min)
```bash
# upgrade AV to a premium tier first; then:
python -c "from backtest.avpull import pull; pull.run('PREMIUM_KEY', './av_data', end_year=2026, end_q=1, per_min=75)"
```
Resumable: re-run after any interruption — completed units in `av_data/manifest.json`
are skipped. Downgrade/cancel the AV plan once the pull is complete.

## 2. Run the backtest (offline, yfinance for prices)
```bash
python -c "from backtest.avpull import run_av_backtest as r; r.main('./av_data', './av_data/results')"
```

## 3. Read the results
`av_data/results/RESULT-av-news.md` and `RESULT-av-transcript.md`. The **held-out
confirmation** row (not the discovery sweep) is the go/no-go: positive rank IC +
monotone quantiles + adequate n ⇒ a tradeable sentiment factor; flat/insignificant
⇒ sentiment-for-sizing unsupported for that factor.

## Caveats (see spec §6, §10)
- Survivorship bias: today's large-caps, not point-in-time membership (rank IC mitigates).
- Transcript dates are quarter-label approximations (lookahead-safe; noisy for
  off-calendar-fiscal names like MU).
- Transcript sentiment may be [0,1] positivity, not signed — interpret rank IC accordingly.
````

- [ ] **Step 2: Commit**

```bash
git add backtest/avpull/README.md
git commit -m "docs(backtest): AV backtest operator runbook"
```

---

## Self-Review

**Spec coverage:**
- §3 reuse engine via FixtureSentimentSource/prices_yf — Tasks 6, 8 ✓
- §4.1 resumable puller (manifest, throttle, 75/min) — Tasks 5, 7 ✓
- §4.2 news weekly relevance-weighted, 2018 trim; transcript non-boilerplate mean — Tasks 2, 3 ✓
- §4.3 run + two reports — Task 8 ✓
- §6 universe — Task 1 ✓
- §8 TDD pure transforms in CI; operator-run network excluded — Tasks 2–6 (CI) vs 7–8 (operator) ✓
- §10.3 quarter-anchor dating — Task 3 (`quarter_to_anchor_date`) ✓
- Calendar snap (no-lookahead alignment) — Task 4 ✓

**Placeholder scan:** none — every code/test step has concrete content; operator-run tasks give exact run commands + expected output instead of CI asserts (justified: network/yfinance, matching `prices_yf`/`scorer_llm`).

**Type consistency:** transforms emit `{"ticker", "points":[{"date","value"}]}` consumed by `load_sentiment_series` (Task 8) ✓; `pending_units`/`run` unit tuples match (`("news",t)`, `("transcript",t,q)`) ✓; `report_markdown(..., source_label=, source_url=)` call in Task 8 matches the Task 6 signature ✓; `snap_series_to_calendar` in/out dict shape matches transform output ✓.

**Open item carried to execution:** Task 7 Step 2 confirms the transcript response shape (presence/absence of a call-date field) — if AV does expose a date, prefer it over `quarter_to_anchor_date` as a fast-follow (not required for v1).
