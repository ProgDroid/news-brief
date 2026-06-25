# GDELT Signal-Validation Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a throwaway offline experiment that measures whether a GDELT-derived Middle-East conflict signal has tradeable predictive power (Information Coefficient / hit-rate) against oil, gold, and equity forward returns — the go/no-go gate for the backlog's GDELT trading-feeder item.

**Architecture:** New code lives under `backtest/gdelt/` (a subpackage of the already-present, dev-only `backtest/`). It parses GDELT 1.0 daily Event CSVs, classifies Middle-East conflict events, folds them into daily accumulators, snaps them *forward* onto the trading calendar (causal), and feeds the **existing** `backtest.run.run_backtest` engine which already does the discovery/held-out IC pipeline. The runner is operator-run (network + yfinance); all pure transforms are unit-tested with hand-built fixtures, no network in CI.

**Tech Stack:** Pure Python stdlib (`csv`, `zipfile`, `urllib`, `bisect`, `dataclasses`) for everything tested; `yfinance` only inside the operator runner (already used by `backtest/prices_yf.py`); reuses `backtest/{series,returns,align,metrics,run}.py`.

## Global Constraints

- **Throwaway spike, not a feature.** No brief wiring, no signal injection into `signals-{date}.json`, no paper-trading hookup, no production config/flag, no GDELT 2.0 real-time puller. Those are deferred until/unless this spike returns a "go".
- **Location: `backtest/gdelt/` only.** A subpackage of `backtest/`. Do **not** create a new top-level module — `backtest/` is intentionally absent from both the Dockerfile `COPY` allowlist and the CI ruff list, so the spike never ships to the runtime image and needs **zero** Dockerfile/workflow changes.
- **All tested code is pure stdlib** — no pandas/numpy/polars. (So no `importorskip` is needed; the CI-no-pandas constraint does not apply here.)
- **`tests/` IS linted by CI ruff; `backtest/` source is NOT.** Still run ruff locally on new `backtest/gdelt/` files (pre-push gate below), because the test files will be CI-linted and must pass.
- **Pre-push / per-commit gate:** `ruff check <changed .py> && ruff format --check <changed .py> && pytest -q`. Stage every file ruff reformats or CI fails.
- **Commit straight to `main`** (solo repo; do not branch first).
- **Make commits via the Bash tool, not PowerShell** (PowerShell prepends a BOM to the commit subject). Run Python/pytest via the PowerShell tool (Bash `pytest` errors "stdin is not a tty" here).
- **GDELT country codes are FIPS 10-4, NOT ISO:** Iraq=`IZ`, Yemen=`YM`, Iran=`IR`, Israel=`IS`, Saudi=`SA`, Syria=`SY`, Lebanon=`LE`.
- **GDELT 1.0 Event rows are tab-delimited, header-less, 58 columns** (post-2013-04). Column indices below are from the GDELT 1.0 codebook and are verified against a live file in Task 4's operator step.

---

### Task 1: Package skeleton + GDELT row parser

**Files:**
- Create: `backtest/gdelt/__init__.py` (empty)
- Create: `backtest/gdelt/events.py`
- Test: `tests/test_gdelt.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `GdeltEvent` frozen dataclass: `date: str` (ISO), `event_root_code: str`, `quad_class: int`, `goldstein: float`, `num_mentions: float`, `avg_tone: float`, `geo_country: str`.
  - `parse_row(fields: list[str]) -> GdeltEvent | None` — typed event from a split row, or `None` if malformed.
  - Column-index constants `COL_SQLDATE`, `COL_EVENT_ROOT_CODE`, `COL_QUAD_CLASS`, `COL_GOLDSTEIN`, `COL_NUM_MENTIONS`, `COL_AVG_TONE`, `COL_ACTION_GEO_COUNTRY`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gdelt.py
from backtest.gdelt.events import GdeltEvent, parse_row


def _row(**over) -> list[str]:
    """A well-formed 58-column GDELT 1.0 Event row (all '0'/'' except set fields)."""
    f = ["0"] * 58
    f[1] = "20260115"   # SQLDATE
    f[28] = "19"        # EventRootCode (fight)
    f[29] = "4"         # QuadClass (material conflict)
    f[30] = "-8.5"      # GoldsteinScale
    f[31] = "12"        # NumMentions
    f[34] = "-6.25"     # AvgTone
    f[51] = "IR"        # ActionGeo_CountryCode (Iran, FIPS)
    for k, v in over.items():
        f[int(k[1:])] = v  # key 'c51' -> index 51
    return f


def test_parse_row_well_formed():
    ev = parse_row(_row())
    assert ev == GdeltEvent(
        date="2026-01-15", event_root_code="19", quad_class=4,
        goldstein=-8.5, num_mentions=12.0, avg_tone=-6.25, geo_country="IR",
    )


def test_parse_row_too_short_returns_none():
    assert parse_row(["0"] * 40) is None


def test_parse_row_bad_numeric_returns_none():
    assert parse_row(_row(c30="not-a-number")) is None


def test_parse_row_bad_date_returns_none():
    assert parse_row(_row(c1="2026")) is None


def test_parse_row_uppercases_and_strips_geo():
    assert parse_row(_row(c51=" iz ")).geo_country == "IZ"
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell tool): `python -m pytest tests/test_gdelt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.gdelt'`.

- [ ] **Step 3: Create the package + implementation**

Create empty `backtest/gdelt/__init__.py`. Then create `backtest/gdelt/events.py`:

```python
# backtest/gdelt/events.py
"""Parse GDELT 1.0 daily Event CSV rows into typed events. Pure stdlib.

GDELT 1.0 Event Table: tab-delimited, header-less, 58 columns (post-2013-04,
SOURCEURL present). Indices below are the GDELT 1.0 codebook order, verified
against a live file in the spike runner's operator step. Country codes are
FIPS 10-4 (NOT ISO): Iraq=IZ, Yemen=YM, Iran=IR, Israel=IS.
"""

from dataclasses import dataclass

COL_SQLDATE = 1            # YYYYMMDD
COL_EVENT_ROOT_CODE = 28   # CAMEO root, "01".."20"
COL_QUAD_CLASS = 29        # 1 verbal-coop 2 material-coop 3 verbal-conflict 4 material-conflict
COL_GOLDSTEIN = 30         # -10..+10 (conflict negative)
COL_NUM_MENTIONS = 31
COL_AVG_TONE = 34          # document tone (usually -10..+10)
COL_ACTION_GEO_COUNTRY = 51  # FIPS 10-4 country code of the event location
_MIN_COLS = 52             # must reach through ActionGeo country


@dataclass(frozen=True)
class GdeltEvent:
    date: str             # ISO YYYY-MM-DD
    event_root_code: str
    quad_class: int
    goldstein: float
    num_mentions: float
    avg_tone: float
    geo_country: str      # FIPS, "" if absent


def _iso_from_sqldate(s: str) -> str | None:
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_row(fields: list[str]) -> GdeltEvent | None:
    """Typed event from a split GDELT row, or None if malformed. Tolerant: a
    short row or any unparseable numeric yields None so one bad line never kills
    a day's parse."""
    if len(fields) < _MIN_COLS:
        return None
    iso = _iso_from_sqldate(fields[COL_SQLDATE])
    if iso is None:
        return None
    try:
        quad = int(fields[COL_QUAD_CLASS])
        gold = float(fields[COL_GOLDSTEIN])
        mentions = float(fields[COL_NUM_MENTIONS])
        tone = float(fields[COL_AVG_TONE])
    except ValueError:
        return None
    return GdeltEvent(
        date=iso,
        event_root_code=fields[COL_EVENT_ROOT_CODE].strip(),
        quad_class=quad,
        goldstein=gold,
        num_mentions=mentions,
        avg_tone=tone,
        geo_country=fields[COL_ACTION_GEO_COUNTRY].strip().upper(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: 5 passed.

- [ ] **Step 5: Lint + commit**

```bash
ruff check backtest/gdelt/events.py tests/test_gdelt.py && ruff format backtest/gdelt/ tests/test_gdelt.py
git add backtest/gdelt/__init__.py backtest/gdelt/events.py tests/test_gdelt.py
git commit -F - <<'EOF'
feat(gdelt-spike): typed GDELT 1.0 Event row parser

Tolerant parse_row -> GdeltEvent (date/quad/goldstein/mentions/tone/geo);
malformed rows return None. Pure stdlib.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Mideast-conflict classification + daily accumulator

**Files:**
- Create: `backtest/gdelt/aggregate.py`
- Test: `tests/test_gdelt.py` (append)

**Interfaces:**
- Consumes: `GdeltEvent` from Task 1.
- Produces:
  - `MIDEAST_FIPS: frozenset[str]`, `CONFLICT_ROOT_CODES: frozenset[str]`.
  - `in_region(ev: GdeltEvent, geo: frozenset[str]) -> bool`.
  - `is_conflict(ev: GdeltEvent) -> bool`.
  - `GdeltDaily` frozen dataclass with SUM fields: `date: str`, `conflict_mentions: float`, `n_conflict_events: int`, `n_region_events: int`, `tone_weighted_sum: float`, `goldstein_weighted_sum: float`, `mention_weight: float`; method `signal(field: str) -> float`.
  - `aggregate_events(date_iso: str, events: list[GdeltEvent], geo: frozenset[str]) -> GdeltDaily`.
  - `fold_daily(events: list[GdeltEvent], geo: frozenset[str]) -> dict[str, GdeltDaily]` (group by event date).
  - `merge_daily(a: GdeltDaily, b: GdeltDaily, *, date_iso: str) -> GdeltDaily` (field-wise sum, re-dated).

- [ ] **Step 1: Write the failing test (append to tests/test_gdelt.py)**

```python
from backtest.gdelt.aggregate import (
    MIDEAST_FIPS,
    GdeltDaily,
    aggregate_events,
    fold_daily,
    is_conflict,
    merge_daily,
)
from backtest.gdelt.events import GdeltEvent


def _ev(geo="IR", root="19", quad=4, gold=-8.0, men=10.0, tone=-5.0, d="2026-01-15"):
    return GdeltEvent(d, root, quad, gold, men, tone, geo)


def test_is_conflict_by_quadclass_or_rootcode():
    assert is_conflict(_ev(quad=3, root="04"))   # verbal conflict
    assert is_conflict(_ev(quad=1, root="19"))   # cooperative quad but fight root
    assert not is_conflict(_ev(quad=2, root="04"))  # material coop, benign root


def test_aggregate_events_region_and_conflict_sums():
    evs = [
        _ev(geo="IR", men=10.0, tone=-4.0, quad=4, root="19"),   # region + conflict
        _ev(geo="IZ", men=5.0, tone=2.0, quad=2, root="04"),     # region, not conflict
        _ev(geo="US", men=99.0, tone=-9.0, quad=4, root="20"),   # out of region -> ignored
    ]
    day = aggregate_events("2026-01-15", evs, MIDEAST_FIPS)
    assert day.date == "2026-01-15"
    assert day.n_region_events == 2
    assert day.n_conflict_events == 1
    assert day.conflict_mentions == 10.0
    assert day.mention_weight == 15.0
    assert day.tone_weighted_sum == 10.0 * -4.0 + 5.0 * 2.0  # -30.0
    assert day.signal("conflict_mentions") == 10.0
    assert day.signal("mean_tone") == -30.0 / 15.0


def test_signal_unknown_field_raises():
    day = aggregate_events("2026-01-15", [_ev()], MIDEAST_FIPS)
    try:
        day.signal("nope")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_fold_daily_groups_by_date():
    evs = [_ev(d="2026-01-15"), _ev(d="2026-01-16")]
    out = fold_daily(evs, MIDEAST_FIPS)
    assert set(out) == {"2026-01-15", "2026-01-16"}


def test_merge_daily_field_wise_sum_and_redate():
    a = GdeltDaily("2026-01-17", 10.0, 1, 2, -30.0, -80.0, 15.0)
    b = GdeltDaily("2026-01-18", 4.0, 1, 1, 8.0, -2.0, 4.0)
    m = merge_daily(a, b, date_iso="2026-01-19")
    assert m.date == "2026-01-19"
    assert m.conflict_mentions == 14.0
    assert m.n_region_events == 3
    assert m.mention_weight == 19.0
    assert m.goldstein_weighted_sum == -82.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.gdelt.aggregate'`.

- [ ] **Step 3: Implement `backtest/gdelt/aggregate.py`**

```python
# backtest/gdelt/aggregate.py
"""Classify GDELT events (Middle-East conflict) and fold them into daily
accumulators. Pure stdlib. Accumulator fields are SUMS so daily records merge
associatively when several calendar days snap onto one trading day."""

from dataclasses import dataclass

from backtest.gdelt.events import GdeltEvent

# FIPS 10-4 codes for the energy/Hormuz theatre. Tunable. NOTE FIPS != ISO:
# Iraq=IZ, Yemen=YM, Saudi=SA, Iran=IR, Israel=IS, Syria=SY, Lebanon=LE.
MIDEAST_FIPS: frozenset[str] = frozenset({"IR", "IZ", "IS", "SA", "SY", "YM", "LE"})

# CAMEO root codes for material/violent conflict: 18 assault, 19 fight,
# 20 unconventional mass violence.
CONFLICT_ROOT_CODES: frozenset[str] = frozenset({"18", "19", "20"})


def in_region(ev: GdeltEvent, geo: frozenset[str]) -> bool:
    return ev.geo_country in geo


def is_conflict(ev: GdeltEvent) -> bool:
    """Material/verbal conflict: QuadClass 3 or 4, OR a violent CAMEO root."""
    return ev.quad_class in (3, 4) or ev.event_root_code in CONFLICT_ROOT_CODES


@dataclass(frozen=True)
class GdeltDaily:
    date: str                      # ISO; calendar day, later snapped to a trading day
    conflict_mentions: float       # sum NumMentions over region CONFLICT events
    n_conflict_events: int
    n_region_events: int
    tone_weighted_sum: float       # sum(AvgTone * NumMentions) over region events
    goldstein_weighted_sum: float  # sum(Goldstein * NumMentions) over region events
    mention_weight: float          # sum(NumMentions) over region events (tone/gold denom)

    def signal(self, field: str) -> float:
        if field == "conflict_mentions":
            return self.conflict_mentions
        w = self.mention_weight
        if field == "mean_tone":
            return self.tone_weighted_sum / w if w else 0.0
        if field == "mean_goldstein":
            return self.goldstein_weighted_sum / w if w else 0.0
        raise ValueError(f"unknown signal field: {field}")


def aggregate_events(
    date_iso: str, events: list[GdeltEvent], geo: frozenset[str]
) -> GdeltDaily:
    cm = mw = tws = gws = 0.0
    nc = nr = 0
    for ev in events:
        if not in_region(ev, geo):
            continue
        nr += 1
        tws += ev.avg_tone * ev.num_mentions
        gws += ev.goldstein * ev.num_mentions
        mw += ev.num_mentions
        if is_conflict(ev):
            cm += ev.num_mentions
            nc += 1
    return GdeltDaily(date_iso, cm, nc, nr, tws, gws, mw)


def fold_daily(
    events: list[GdeltEvent], geo: frozenset[str]
) -> dict[str, GdeltDaily]:
    """Group region events by their event date into daily accumulators."""
    by_date: dict[str, list[GdeltEvent]] = {}
    for ev in events:
        by_date.setdefault(ev.date, []).append(ev)
    return {d: aggregate_events(d, evs, geo) for d, evs in by_date.items()}


def merge_daily(a: GdeltDaily, b: GdeltDaily, *, date_iso: str) -> GdeltDaily:
    return GdeltDaily(
        date=date_iso,
        conflict_mentions=a.conflict_mentions + b.conflict_mentions,
        n_conflict_events=a.n_conflict_events + b.n_conflict_events,
        n_region_events=a.n_region_events + b.n_region_events,
        tone_weighted_sum=a.tone_weighted_sum + b.tone_weighted_sum,
        goldstein_weighted_sum=a.goldstein_weighted_sum + b.goldstein_weighted_sum,
        mention_weight=a.mention_weight + b.mention_weight,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: all passed (5 from Task 1 + 5 new).

- [ ] **Step 5: Lint + commit**

```bash
ruff check backtest/gdelt/aggregate.py tests/test_gdelt.py && ruff format backtest/gdelt/ tests/test_gdelt.py
git add backtest/gdelt/aggregate.py tests/test_gdelt.py
git commit -F - <<'EOF'
feat(gdelt-spike): Mideast-conflict classification + daily accumulator

is_conflict (QuadClass 3/4 or CAMEO root 18-20), region filter (FIPS),
GdeltDaily sum-accumulator + fold_daily/merge_daily (associative merge).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Forward calendar snap + signal series

**Files:**
- Create: `backtest/gdelt/snap.py`
- Test: `tests/test_gdelt.py` (append)

**Interfaces:**
- Consumes: `GdeltDaily`, `merge_daily` from Task 2; `SentimentSeries`, `SentimentPoint` from `backtest.series`.
- Produces:
  - `snap_forward(dailies: dict[str, GdeltDaily], trading_days: set[str], *, max_gap: int = 4) -> dict[str, GdeltDaily]` — map each calendar-day record to the first trading day `>=` its date, merging collisions; drop if the next session is more than `max_gap` days away.
  - `to_sentiment_series(snapped: dict[str, GdeltDaily], field: str, *, label: str = "GDELT") -> SentimentSeries`.

- [ ] **Step 1: Write the failing test (append)**

```python
from backtest.gdelt.aggregate import GdeltDaily
from backtest.gdelt.snap import snap_forward, to_sentiment_series


def _daily(d, cm=1.0, mw=2.0, tws=-4.0):
    return GdeltDaily(d, cm, 1, 1, tws, -3.0, mw)


def test_snap_forward_weekend_folds_into_monday():
    trading = {"2026-01-16", "2026-01-20"}  # Fri, then Tue (Mon 19th holiday)
    dailies = {
        "2026-01-17": _daily("2026-01-17", cm=3.0, mw=3.0),  # Sat
        "2026-01-18": _daily("2026-01-18", cm=5.0, mw=5.0),  # Sun
        "2026-01-20": _daily("2026-01-20", cm=2.0, mw=2.0),  # Tue (trading)
    }
    out = snap_forward(dailies, trading)
    assert set(out) == {"2026-01-20"}                 # all three land on Tue
    assert out["2026-01-20"].conflict_mentions == 10.0
    assert out["2026-01-20"].date == "2026-01-20"


def test_snap_forward_trading_day_maps_to_itself():
    out = snap_forward({"2026-01-16": _daily("2026-01-16", cm=7.0)}, {"2026-01-16"})
    assert out["2026-01-16"].conflict_mentions == 7.0


def test_snap_forward_drops_when_no_session_within_gap():
    # next session is 10 days away -> dropped (max_gap default 4)
    out = snap_forward({"2026-01-01": _daily("2026-01-01")}, {"2026-01-15"})
    assert out == {}


def test_to_sentiment_series_sorted_and_field_applied():
    snapped = {
        "2026-01-20": _daily("2026-01-20", cm=2.0),
        "2026-01-16": _daily("2026-01-16", cm=9.0),
    }
    s = to_sentiment_series(snapped, "conflict_mentions", label="USO:cm")
    assert s.ticker == "USO:cm"
    assert [p.date for p in s.points] == ["2026-01-16", "2026-01-20"]
    assert s.points[0].value == 9.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.gdelt.snap'`.

- [ ] **Step 3: Implement `backtest/gdelt/snap.py`**

```python
# backtest/gdelt/snap.py
"""Snap GDELT calendar-day records FORWARD onto the trading calendar and reduce
to a SentimentSeries. Forward (not backward) snap is causal: weekend/holiday
news becomes actionable at the NEXT trading session, so a trading day's signal
never contains information dated after that day's close (no look-ahead)."""

import bisect
from dataclasses import replace
from datetime import date

from backtest.gdelt.aggregate import GdeltDaily, merge_daily
from backtest.series import SentimentPoint, SentimentSeries


def snap_forward(
    dailies: dict[str, GdeltDaily], trading_days: set[str], *, max_gap: int = 4
) -> dict[str, GdeltDaily]:
    cal = sorted(trading_days)
    out: dict[str, GdeltDaily] = {}
    for d, rec in dailies.items():
        i = bisect.bisect_left(cal, d)  # first trading day >= d
        if i >= len(cal):
            continue
        tday = cal[i]
        if (date.fromisoformat(tday) - date.fromisoformat(d)).days > max_gap:
            continue
        out[tday] = (
            merge_daily(out[tday], rec, date_iso=tday)
            if tday in out
            else replace(rec, date=tday)
        )
    return out


def to_sentiment_series(
    snapped: dict[str, GdeltDaily], field: str, *, label: str = "GDELT"
) -> SentimentSeries:
    pts = tuple(
        SentimentPoint(d, rec.signal(field)) for d, rec in sorted(snapped.items())
    )
    return SentimentSeries(ticker=label, points=pts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: all passed (10 prior + 4 new).

- [ ] **Step 5: Lint + commit**

```bash
ruff check backtest/gdelt/snap.py tests/test_gdelt.py && ruff format backtest/gdelt/ tests/test_gdelt.py
git add backtest/gdelt/snap.py tests/test_gdelt.py
git commit -F - <<'EOF'
feat(gdelt-spike): causal forward-snap onto trading calendar + signal series

snap_forward maps weekend/holiday records to the next session (no look-ahead),
merging collisions; to_sentiment_series reduces to a SentimentSeries by field.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: GDELT fetch + unzip with on-disk cache

**Files:**
- Create: `backtest/gdelt/fetch.py`
- Test: `tests/test_gdelt.py` (append)

**Interfaces:**
- Consumes: nothing internal.
- Produces:
  - `events_url(date_yyyymmdd: str) -> str`.
  - `unzip_to_rows(zip_bytes: bytes) -> list[list[str]]` (pure; first zip member, tab-split, blank rows dropped).
  - `fetch_day_rows(date_yyyymmdd: str, *, cache_dir: str | None = None, timeout: float = 60.0) -> list[list[str]]` (operator-run network with on-disk cache; `[]` on 404/any network error).

- [ ] **Step 1: Write the failing test (append)**

```python
import io
import zipfile

from backtest.gdelt.fetch import events_url, fetch_day_rows, unzip_to_rows


def _make_zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_events_url():
    assert events_url("20260115") == (
        "http://data.gdeltproject.org/events/20260115.export.CSV.zip"
    )


def test_unzip_to_rows_tab_split_drops_blanks():
    z = _make_zip("20260115.export.CSV", "a\tb\tc\n\nd\te\tf\n")
    assert unzip_to_rows(z) == [["a", "b", "c"], ["d", "e", "f"]]


def test_fetch_day_rows_cache_hit_no_network(tmp_path):
    # Pre-seed the cache; fetch must read it and never touch the network.
    z = _make_zip("20260115.export.CSV", "x\ty\tz\n")
    (tmp_path / "20260115.export.CSV.zip").write_bytes(z)
    rows = fetch_day_rows("20260115", cache_dir=str(tmp_path))
    assert rows == [["x", "y", "z"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.gdelt.fetch'`.

- [ ] **Step 3: Implement `backtest/gdelt/fetch.py`**

```python
# backtest/gdelt/fetch.py
"""Download + unzip GDELT 1.0 daily Event files, with an on-disk cache. The
network fetch is operator-run (not exercised in CI); unzip_to_rows and the
cache-hit path are pure/deterministic and tested. 404 / any network error
-> [] (a missing day skips, never raises)."""

import csv
import io
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_BASE = "http://data.gdeltproject.org/events"


def events_url(date_yyyymmdd: str) -> str:
    return f"{_BASE}/{date_yyyymmdd}.export.CSV.zip"


def unzip_to_rows(zip_bytes: bytes) -> list[list[str]]:
    """First member of the zip parsed as tab-delimited rows (blank rows dropped)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    return [r for r in csv.reader(io.StringIO(text), delimiter="\t") if r]


def fetch_day_rows(
    date_yyyymmdd: str, *, cache_dir: str | None = None, timeout: float = 60.0
) -> list[list[str]]:
    """Rows for one GDELT day. Caches the raw zip under cache_dir so re-runs are
    free. Returns [] on 404 (day not published) or any network/zip error."""
    cache = Path(cache_dir) / f"{date_yyyymmdd}.export.CSV.zip" if cache_dir else None
    if cache and cache.exists():
        data = cache.read_bytes()
    else:
        try:
            with urlopen(events_url(date_yyyymmdd), timeout=timeout) as r:
                data = r.read()
        except (HTTPError, URLError, TimeoutError):
            return []
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
    try:
        return unzip_to_rows(data)
    except (zipfile.BadZipFile, OSError):
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: all passed (14 prior + 3 new).

- [ ] **Step 5: Lint + commit**

```bash
ruff check backtest/gdelt/fetch.py tests/test_gdelt.py && ruff format backtest/gdelt/ tests/test_gdelt.py
git add backtest/gdelt/fetch.py tests/test_gdelt.py
git commit -F - <<'EOF'
feat(gdelt-spike): cached GDELT 1.0 daily fetch + unzip

events_url, pure unzip_to_rows (tab-split), fetch_day_rows with on-disk cache;
graceful [] on 404/network error. Cache-hit path unit-tested (no network).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Operator runner + summary grid + FINDINGS template

**Files:**
- Create: `backtest/gdelt/run_gdelt_spike.py`
- Create: `backtest/gdelt/FINDINGS.md`
- Test: `tests/test_gdelt.py` (append)

**Interfaces:**
- Consumes: everything above; `backtest.prices_yf.fetch_price_series`, `backtest.run.run_backtest`, `backtest.run.report_markdown`.
- Produces (pure, tested):
  - `daterange(start: str, end: str) -> list[str]` — inclusive YYYYMMDD list.
  - `summarize(results: list[dict]) -> str` — compact go/no-go markdown grid. Each `result` dict has keys `symbol, field, mode, expected_sign, result` (where `result` is a `run_backtest` return).
- Produces (operator-run, untested): `build_dailies(...)`, `main(...)`, `__main__` CLI.

- [ ] **Step 1: Write the failing test (append)**

```python
from backtest.gdelt.run_gdelt_spike import daterange, summarize


def test_daterange_inclusive():
    assert daterange("2026-01-14", "2026-01-16") == ["20260114", "20260115", "20260116"]


def _res(ic, hr=0.55, n=80, best=5):
    return {"confirmation": {"ic": ic, "hit_rate": hr, "n": n}, "best_horizon": best}


def test_summarize_marks_primary_cell_sign_ok():
    results = [
        {"symbol": "USO", "field": "conflict_mentions", "mode": "level",
         "expected_sign": +1, "result": _res(0.08)},   # correct sign, |IC|>0.03 -> yes
        {"symbol": "SPY", "field": "conflict_mentions", "mode": "level",
         "expected_sign": -1, "result": _res(0.10)},    # wrong sign -> no
        {"symbol": "USO", "field": "mean_tone", "mode": "delta",
         "expected_sign": +1, "result": _res(0.20)},    # non-primary cell -> dash
    ]
    grid = summarize(results)
    lines = grid.splitlines()
    uso = next(line for line in lines if "USO | conflict_mentions | level" in line)
    spy = next(line for line in lines if "SPY | conflict_mentions | level" in line)
    tone = next(line for line in lines if "USO | mean_tone | delta" in line)
    assert uso.rstrip().endswith("yes |")
    assert spy.rstrip().endswith("no |")
    assert tone.rstrip().endswith("— |")  # em dash for non-primary cells
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gdelt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.gdelt.run_gdelt_spike'`.

- [ ] **Step 3: Implement `backtest/gdelt/run_gdelt_spike.py`**

```python
# backtest/gdelt/run_gdelt_spike.py
"""Operator-run GDELT signal-validation spike. Pulls GDELT 1.0 daily events over
a window, builds a Middle-East conflict signal, and measures its IC / hit-rate
against oil/gold/equity forward returns via the existing backtest engine. NOT
imported by the cron app or CI. Network + yfinance required.

Run:
  python -m backtest.gdelt.run_gdelt_spike \\
      --start 2025-09-15 --end 2026-06-20 \\
      --cache .gdelt_cache --out backtest/gdelt/out
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backtest.gdelt.aggregate import (
    MIDEAST_FIPS,
    GdeltDaily,
    fold_daily,
    merge_daily,
)
from backtest.gdelt.events import parse_row
from backtest.gdelt.fetch import fetch_day_rows
from backtest.gdelt.snap import snap_forward, to_sentiment_series
from backtest.prices_yf import fetch_price_series
from backtest.run import report_markdown, run_backtest

HORIZONS = [1, 3, 5, 10]
# (yfinance symbol, expected IC sign for the PRIMARY conflict_mentions signal)
INSTRUMENTS = [("USO", +1), ("GLD", +1), ("SPY", -1), ("ITA", +1)]
PRIMARY_FIELD = "conflict_mentions"
FIELDS = ["conflict_mentions", "mean_tone", "mean_goldstein"]
MODES = ["level", "delta"]
_IC_FLOOR = 0.03  # |held-out IC| below this is treated as noise for the sign check


def daterange(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range((d1 - d0).days + 1)]


def build_dailies(start: str, end: str, cache: str | None) -> dict[str, GdeltDaily]:
    """Pull every day in [start, end] and fold into calendar-day accumulators."""
    acc: dict[str, GdeltDaily] = {}
    for ymd in daterange(start, end):
        events = [ev for ev in map(parse_row, fetch_day_rows(ymd, cache_dir=cache)) if ev]
        for d, rec in fold_daily(events, MIDEAST_FIPS).items():
            acc[d] = merge_daily(acc[d], rec, date_iso=d) if d in acc else rec
    return acc


def _sign_ok(r: dict) -> str:
    conf = r["result"]["confirmation"]
    if not conf or r["field"] != PRIMARY_FIELD or r["mode"] != "level":
        return "—"  # em dash: not a pre-registered decision cell
    ic = conf["ic"]
    correct = (ic > 0) == (r["expected_sign"] > 0)
    return "yes" if correct and abs(ic) > _IC_FLOOR else "no"


def summarize(results: list[dict]) -> str:
    lines = [
        "| instrument | field | mode | best h | held-out IC | hit rate | n | sign ok |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        conf = r["result"]["confirmation"]
        ic = f"{conf['ic']:.4f}" if conf else "n/a"
        hr = f"{conf['hit_rate']:.2%}" if conf else "n/a"
        n = conf["n"] if conf else 0
        lines.append(
            f"| {r['symbol']} | {r['field']} | {r['mode']} "
            f"| {r['result']['best_horizon']} | {ic} | {hr} | {n} | {_sign_ok(r)} |"
        )
    return "\n".join(lines)


def main(start: str, end: str, cache: str | None, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dailies = build_dailies(start, end, cache)
    print(f"GDELT calendar days with region events: {len(dailies)}")

    results: list[dict] = []
    for sym, sign in INSTRUMENTS:
        try:
            prices = fetch_price_series(sym, start, end)
        except Exception as e:  # one flaky symbol must not kill the run
            print(f"price fetch failed for {sym}: {e}; skipping")
            continue
        snapped = snap_forward(dailies, set(prices.closes.keys()))
        for field in FIELDS:
            series = to_sentiment_series(snapped, field, label=f"{sym}:{field}")
            for mode in MODES:
                res = run_backtest(
                    {sym: series}, {sym: prices}, HORIZONS, mode=mode, standardize=True
                )
                (out / f"RESULT-{sym}-{field}-{mode}.md").write_text(
                    report_markdown(
                        res,
                        source_label=f"GDELT MidEast conflict ({field}/{mode}) vs {sym}",
                        source_url="https://www.gdeltproject.org",
                    )
                )
                results.append(
                    {"symbol": sym, "field": field, "mode": mode,
                     "expected_sign": sign, "result": res}
                )

    grid = summarize(results)
    (out / "SUMMARY.md").write_text(grid)
    (out / "SUMMARY.json").write_text(
        json.dumps(
            [
                {k: r[k] for k in ("symbol", "field", "mode", "expected_sign")}
                | {"confirmation": r["result"]["confirmation"],
                   "best_horizon": r["result"]["best_horizon"]}
                for r in results
            ],
            indent=2,
        )
    )
    print(grid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--cache", default=".gdelt_cache")
    ap.add_argument("--out", default="backtest/gdelt/out")
    a = ap.parse_args()
    main(a.start, a.end, a.cache, a.out)
```

- [ ] **Step 4: Create `backtest/gdelt/FINDINGS.md` (template to fill after the operator run in Task 6)**

```markdown
# GDELT Signal-Validation Spike — Findings

**Window:** _<start>_ → _<end>_  ·  **GDELT region-event days:** _<n>_
**Engine:** `backtest.run.run_backtest` (temporal 50/50 discovery/held-out split, standardized, horizons 1/3/5/10d)

## Pre-registered decision rule (set BEFORE running)
- **Primary cell:** signal = `conflict_mentions`, mode = `level`, per instrument.
- **GO** (build the GDELT 2.0 real-time feeder) requires, on the **held-out** split:
  correct-sign IC with `|IC| > 0.03` on **both** `USO` and `GLD`, hit-rate > ~53%, n ≥ ~120.
  `SPY` correct sign (negative) is confirmatory, not required.
- **NO-GO / SKIP:** otherwise. Everything outside the primary cell (mean_tone,
  mean_goldstein, delta mode) is **exploratory robustness only** — do not let an
  incidental hit in a non-primary cell flip the decision (multiple-comparison risk:
  ~24 cells swept).

## Results
_Paste `SUMMARY.md` here._

## Decision
- [ ] GO  — proceed to 2.0 real-time puller (separate spec/plan)
- [ ] NO-GO / SKIP — close backlog item #2

**Rationale:** _<one paragraph: which cells fired, signs, n, and why it clears/fails the bar>_
```

- [ ] **Step 5: Run tests + lint + commit**

```bash
python -m pytest tests/test_gdelt.py -q   # expect all passed (17 prior + 2 new)
ruff check backtest/gdelt/run_gdelt_spike.py tests/test_gdelt.py && ruff format backtest/gdelt/ tests/test_gdelt.py
git add backtest/gdelt/run_gdelt_spike.py backtest/gdelt/FINDINGS.md tests/test_gdelt.py
git commit -F - <<'EOF'
feat(gdelt-spike): operator runner, summary grid, FINDINGS template

build_dailies -> per-instrument run_backtest (no pooling across opposite-sign
instruments); summarize marks the pre-registered primary cell (conflict_mentions
/level) sign-ok. Pure daterange/summarize unit-tested; main() operator-run.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Operator run — verify schema, execute spike, record decision

This task runs the real experiment. No new code; it produces the go/no-go answer and closes the loop. Requires network + `yfinance` installed; run Python via the PowerShell tool.

- [ ] **Step 1: Verify the live GDELT 1.0 schema (guards the column indices)**

Download one recent day and confirm the assumptions in `events.py` before trusting 180 files. Run:

```python
python -c "from backtest.gdelt.fetch import fetch_day_rows; \
rows=fetch_day_rows('20260601', cache_dir='.gdelt_cache'); \
print('rows', len(rows)); print('cols', len(rows[0]) if rows else 0); \
r=rows[0]; print('date', r[1], 'root', r[28], 'quad', r[29], 'gold', r[30], 'geo', r[51])"
```

Expected: `rows` in the hundreds-of-thousands; `cols` == 58; `date` looks like `YYYYMMDD`; `root` is a 1–2 digit code; `quad` ∈ {1,2,3,4}; `geo` is a 2-char code or empty.
If `cols` != 58 or fields don't line up, **stop** and fix the `COL_*` indices in `backtest/gdelt/events.py` against the GDELT 1.0 Event codebook before continuing.

- [ ] **Step 2: Run the spike over a ~9-month window**

```bash
python -m backtest.gdelt.run_gdelt_spike --start 2025-09-15 --end 2026-06-20 --cache .gdelt_cache --out backtest/gdelt/out
```

Notes: the first run downloads ~270 daily zips (cached under `.gdelt_cache`, so re-runs are free); expect several minutes. Confirm the printed `region-event days` count and `n` per cell are ≥ ~120 (widen the window if not).

- [ ] **Step 3: Record findings + make the call**

Fill `backtest/gdelt/FINDINGS.md`: paste `backtest/gdelt/out/SUMMARY.md`, apply the **pre-registered decision rule**, tick GO or NO-GO, write the rationale (cells that fired, signs, n).

- [ ] **Step 4: Update the backlog memory STATUS**

Edit `C:\Users\Nando Ferreira\.claude\projects\G--pythonDev-news-brief\memory\external-geo-dashboards-backlog.md`: change item #2's `[OPEN]` to `[DONE <date>: GO — next: build 2.0 real-time feeder (new spec)]` or `[DONE <date>: NO-GO/SKIP — held-out IC null, closed]`, and update the top-line "NEXT OPEN" pointer to item #3 (multi-source corroboration). Add a one-line index entry to `MEMORY.md` only if a new standalone memory is created.

- [ ] **Step 5: Commit the findings (do NOT commit `.gdelt_cache/` or `backtest/gdelt/out/`)**

Add `.gdelt_cache/` and `backtest/gdelt/out/` to `.gitignore` (raw data + generated reports are not source). Commit only `FINDINGS.md` + the `.gitignore` change:

```bash
git add .gitignore backtest/gdelt/FINDINGS.md
git commit -F - <<'EOF'
docs(gdelt-spike): record signal-validation findings + go/no-go decision

<one line: GO or NO-GO and the headline IC numbers>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

- [ ] **Step 6: Capture the learning**

Invoke the capture-learnings skill (or update the backlog memory) with the outcome: the IC result, that GDELT 1.0 daily was used for cheap validation, the FIPS-vs-ISO and forward-snap gotchas, and the go/no-go. If GO, note that the 2.0 real-time puller is the next build (own spec).

---

## Self-Review

**1. Spec coverage:**
- Hypothesis (Mideast conflict → oil/gold up, equities down) → Tasks 2 (region+conflict), 3 (signal), 5 (`INSTRUMENTS` USO/GLD/SPY/ITA with expected signs). ✓
- Data source = GDELT 1.0 daily files → Task 4 (`events_url`, `fetch_day_rows`). ✓
- Method = reuse `backtest/` IC machinery → Task 5 (`run_backtest` + `report_markdown`, one instrument per call). ✓
- Scope (under `backtest/gdelt/`, no Docker/workflow change, light tests, pure stdlib) → Global Constraints + every task. ✓
- Go/no-go criteria → Task 5 `summarize` sign-ok on the pre-registered cell + Task 6 FINDINGS decision rule. ✓
- Deliverables (spike code, tests, FINDINGS, backlog STATUS, learning) → Tasks 1–6. ✓
- Honest-null expectation → FINDINGS decision rule explicitly allows NO-GO/SKIP. ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"write tests for the above" — every code step shows full code; the only blanks are operator-filled result values in `FINDINGS.md` (correct: those are experimental outputs, not plan gaps). ✓

**3. Type consistency:** `GdeltEvent` fields (Task 1) consumed unchanged by `aggregate_events`/`is_conflict` (Task 2). `GdeltDaily` sum-fields defined once (Task 2) and merged field-for-field by `merge_daily` (Task 2) and `snap_forward` (Task 3). `fold_daily`/`merge_daily`/`snap_forward`/`to_sentiment_series`/`fetch_day_rows`/`unzip_to_rows`/`events_url`/`daterange`/`summarize` names are identical at definition and call sites. `run_backtest({sym: series}, {sym: prices}, ...)` matches its real signature (`dict[str, SentimentSeries]`, `dict[str, PriceSeries]`, `horizons`, `mode=`, `standardize=`) verified in `backtest/run.py`. ✓
