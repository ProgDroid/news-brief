# Historical Crossing-Date Checkpoint Prices — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record each paper `1w`/`2w`/`4w` checkpoint at the close on its true crossing date (`entry_date + threshold`) for equity/index/crypto positions, falling back to the current mark price (flagged) when no historical price is available.

**Architecture:** Extend the existing live Yahoo/Kraken REST pricers in `trading.py` with dependency-free historical-series fetchers (`requests`-only, no `yfinance`). A dispatcher returns a `{date: close}` map per position; a pure `_snap_close` picks the close on/before each crossing date. `_record_checkpoints` is rewritten to price each checkpoint from that map (or fall back), and `mark_to_market` fetches the map once per position only when a checkpoint newly crosses.

**Tech Stack:** Python 3, `requests`, `pytest` (monkeypatch). No new dependencies.

## Global Constraints

- Live trading path stays `requests`-only — **do NOT import `yfinance`** into `trading.py` (kept out of the cron/CI image by design).
- Network fetchers are not exercised in CI — only their parse/transform logic is tested (monkeypatched `requests.get`), matching the existing `_yahoo_quote`/`fetch_kraken_price` tests.
- Checkpoint schema change is **additive**: new field `price_basis: "historical" | "current"`. No book migration; readers treat absent as legacy/current.
- Fail-safe: any historical-fetch failure degrades to the current mark price — never blocks a checkpoint or stalls the 4w close.
- All new tests live in `tests/test_checkpoint_backfill.py`.
- `_signal_return(direction, entry, price)`, `_yahoo_format_symbol(base, market)`, `_parse_symbol(symbol) -> (base, market) | None`, `_YAHOO_HEADERS`, `PAPER_HORIZONS = {"1w":7,"2w":14,"4w":28}`, `PAPER_CLOSE_HORIZON = "4w"`, and `from datetime import datetime, timezone, timedelta` already exist in `trading.py`.
- Run `ruff check . && ruff format --check . && pytest` before any push (project pre-push gate). Commit straight to `main` (solo repo, no branch).
- Commit via the **Bash tool** (PowerShell prepends a BOM to commit subjects here).

---

## File Structure

- `trading.py` — all new functions + the `_record_checkpoints` rewrite + `mark_to_market`/`_mtm_prediction` wiring. No new module (avoids Dockerfile/workflow allowlist changes).
- `tests/test_checkpoint_backfill.py` — new, all tests for this feature (local `_FakeJsonResp` + payload builders).

New symbols in `trading.py`:
- `_snap_close(closes, target_date) -> float | None` — pure.
- `_yahoo_closes(yahoo_symbol, start, end) -> dict[str, float]` — REST history (place after `_yahoo_fetch`).
- `_kraken_closes(pair, since) -> dict[str, float]` — REST history (place after `fetch_kraken_price`).
- `historical_closes(asset_class, instrument, start, end) -> dict[str, float]` — dispatcher (place after `fetch_price`).
- `_has_new_crossing(p, days_open) -> bool` — fetch gate (place just before `_record_checkpoints`).
- `_record_checkpoints(p, today_str, price, ret, days_open, closes)` — rewritten (new trailing `closes` param).

---

### Task 1: `_snap_close` pure helper

**Files:**
- Modify: `trading.py` (add `_snap_close` just before `_record_checkpoints`, ~line 1531)
- Test: `tests/test_checkpoint_backfill.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `_snap_close(closes: dict[str, float], target_date: str) -> float | None` — returns the close for the latest date key `<= target_date`, or `None` if none.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checkpoint_backfill.py`:

```python
# tests/test_checkpoint_backfill.py
import trading


def test_snap_close_exact_date_hit():
    closes = {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0}
    assert trading._snap_close(closes, "2026-06-02") == 11.0


def test_snap_close_weekend_snaps_back_to_prior_close():
    # 2026-06-06 is a Saturday with no close; snap to Friday 2026-06-05.
    closes = {"2026-06-04": 10.0, "2026-06-05": 11.0, "2026-06-08": 12.0}
    assert trading._snap_close(closes, "2026-06-06") == 11.0


def test_snap_close_target_before_history_returns_none():
    closes = {"2026-06-10": 10.0}
    assert trading._snap_close(closes, "2026-06-01") is None


def test_snap_close_empty_map_returns_none():
    assert trading._snap_close({}, "2026-06-01") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute '_snap_close'`

- [ ] **Step 3: Write minimal implementation**

Add to `trading.py` just before `_record_checkpoints`:

```python
def _snap_close(closes: dict[str, float], target_date: str) -> float | None:
    """Close on the last trading day on/before target_date (None if none <= target).

    Lexical comparison is valid for ISO YYYY-MM-DD. Snaps a weekend/holiday
    crossing date back to the prior available close; returns None when the map is
    empty or every date is after target (target precedes available history).
    """
    candidates = [d for d in closes if d <= target_date]
    if not candidates:
        return None
    return closes[max(candidates)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_checkpoint_backfill.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): _snap_close pure helper for crossing-date price lookup

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `_yahoo_closes` historical series fetcher

**Files:**
- Modify: `trading.py` (add `_yahoo_closes` after `_yahoo_fetch`, ~line 326)
- Test: `tests/test_checkpoint_backfill.py`

**Interfaces:**
- Consumes: `_YAHOO_HEADERS`, `requests`, `datetime`/`timezone`/`timedelta`, `log`.
- Produces: `_yahoo_closes(yahoo_symbol: str, start: str, end: str) -> dict[str, float]` — `{date: close}` over `[start, end]` inclusive, GBp converted to GBP, `{}` on any failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checkpoint_backfill.py` (add the `_FakeJsonResp` helper + a Yahoo-history payload builder at the top of the file, below the import):

```python
from datetime import datetime, timezone


class _FakeJsonResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _ts(date_str):
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _yahoo_hist_payload(rows, currency="USD"):
    # rows: list of (date_str, close)
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency},
                    "timestamp": [_ts(d) for d, _ in rows],
                    "indicators": {"quote": [{"close": [c for _, c in rows]}]},
                }
            ],
            "error": None,
        }
    }
```

Then the tests:

```python
def test_yahoo_closes_parses_series(monkeypatch):
    rows = [("2026-06-01", 10.0), ("2026-06-02", 11.0), ("2026-06-03", 12.0)]
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp(_yahoo_hist_payload(rows))
    )
    out = trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03")
    assert out == {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0}


def test_yahoo_closes_converts_pence_to_pounds(monkeypatch):
    rows = [("2026-06-01", 2750.0)]
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp(_yahoo_hist_payload(rows, currency="GBp")),
    )
    out = trading._yahoo_closes("RR.L", "2026-06-01", "2026-06-01")
    assert out == {"2026-06-01": 27.5}


def test_yahoo_closes_skips_null_closes(monkeypatch):
    payload = _yahoo_hist_payload([("2026-06-01", 10.0), ("2026-06-02", 11.0)])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _FakeJsonResp(payload))
    out = trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-02")
    assert out == {"2026-06-01": 10.0}


def test_yahoo_closes_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=429)
    )
    assert trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03") == {}


def test_yahoo_closes_empty_result_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp({"chart": {"result": None, "error": None}}),
    )
    assert trading._yahoo_closes("AAPL", "2026-06-01", "2026-06-03") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k yahoo_closes -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute '_yahoo_closes'`

- [ ] **Step 3: Write minimal implementation**

Add to `trading.py` after `_yahoo_fetch`:

```python
def _yahoo_closes(yahoo_symbol: str, start: str, end: str) -> dict[str, float]:
    """Daily closes for an already-formatted Yahoo symbol over [start, end] inclusive.

    Same v8/chart endpoint as _yahoo_fetch, but a date range (period1/period2).
    Returns {date: close} with the GBp->GBP /100 conversion applied, or {} on any
    network/parse failure — callers fall back to the current mark price.
    """
    try:
        p1 = int(
            datetime.strptime(start, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        # +1 day so the end date itself falls inside Yahoo's half-open range.
        p2 = int(
            (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return {}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "period1": p1, "period2": p2},
            headers=_YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Yahoo history failed for {yahoo_symbol}: {e}")
        return {}
    result = (data.get("chart") or {}).get("result")
    if not result:
        return {}
    node = result[0]
    meta = node.get("meta") or {}
    timestamps = node.get("timestamp") or []
    try:
        closes = node["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return {}
    pence = meta.get("currency") == "GBp"
    out: dict[str, float] = {}
    for ts, c in zip(timestamps, closes):
        if c is None:
            continue
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        if pence:
            val /= 100.0
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out[date] = val
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k yahoo_closes -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): _yahoo_closes REST historical series fetcher

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: `_kraken_closes` historical series fetcher

**Files:**
- Modify: `trading.py` (add `_kraken_closes` after `fetch_kraken_price`, ~line 185)
- Test: `tests/test_checkpoint_backfill.py`

**Interfaces:**
- Consumes: `requests`, `datetime`/`timezone`, `log`.
- Produces: `_kraken_closes(pair: str, since: str) -> dict[str, float]` — `{date: close}` from daily OHLC, `{}` on failure.

- [ ] **Step 1: Write the failing tests**

Append the Kraken payload builder + tests to `tests/test_checkpoint_backfill.py`:

```python
def _kraken_ohlc_payload(rows, pair_key="XXBTZUSD", error=None):
    # rows: list of (date_str, close); Kraken row = [time,o,h,l,c,vwap,vol,count]
    candles = [
        [_ts(d), "0", "0", "0", str(c), "0", "0", 0] for d, c in rows
    ]
    return {"error": error or [], "result": {pair_key: candles, "last": 0}}


def test_kraken_closes_parses_series(monkeypatch):
    rows = [("2026-06-01", 60000.0), ("2026-06-02", 61000.0)]
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp(_kraken_ohlc_payload(rows))
    )
    out = trading._kraken_closes("XBTUSD", "2026-06-01")
    assert out == {"2026-06-01": 60000.0, "2026-06-02": 61000.0}


def test_kraken_closes_error_array_returns_empty(monkeypatch):
    payload = _kraken_ohlc_payload([], error=["EGeneral:Invalid"])
    monkeypatch.setattr(trading.requests, "get", lambda *a, **k: _FakeJsonResp(payload))
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}


def test_kraken_closes_empty_result_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeJsonResp({"error": [], "result": {"last": 0}}),
    )
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}


def test_kraken_closes_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeJsonResp({}, status=500)
    )
    assert trading._kraken_closes("XBTUSD", "2026-06-01") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k kraken_closes -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute '_kraken_closes'`

- [ ] **Step 3: Write minimal implementation**

Add to `trading.py` after `fetch_kraken_price`:

```python
def _kraken_closes(pair: str, since: str) -> dict[str, float]:
    """Daily closes for a Kraken pair from `since` onward, via /0/public/OHLC.

    interval=1440 = daily candles; OHLC close is field index 4. Kraken keys the
    result by canonical pair name plus a 'last' int, so take the single list-valued
    entry. Returns {date: close}, or {} on error array / empty / parse failure —
    callers fall back to the current mark price.
    """
    try:
        since_ts = int(
            datetime.strptime(since, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return {}
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440&since={since_ts}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken history failed for {pair}: {e}")
        return {}
    if data.get("error"):
        log.warning(f"Kraken history error for {pair}: {data['error']}")
        return {}
    result = data.get("result") or {}
    candles = next(
        (v for k, v in result.items() if k != "last" and isinstance(v, list)), None
    )
    if not candles:
        return {}
    out: dict[str, float] = {}
    for row in candles:
        try:
            date = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            out[date] = float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k kraken_closes -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): _kraken_closes REST historical OHLC fetcher

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: `historical_closes` dispatcher

**Files:**
- Modify: `trading.py` (add `historical_closes` after `fetch_price`, ~line 818)
- Test: `tests/test_checkpoint_backfill.py`

**Interfaces:**
- Consumes: `_yahoo_closes`, `_kraken_closes`, `_parse_symbol`, `_yahoo_format_symbol`.
- Produces: `historical_closes(asset_class: str, instrument: str, start: str, end: str) -> dict[str, float]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checkpoint_backfill.py`:

```python
def test_historical_closes_equity_resolves_yahoo_symbol(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        trading,
        "_yahoo_closes",
        lambda sym, s, e: seen.setdefault("sym", sym) or {"2026-06-01": 10.0},
    )
    out = trading.historical_closes("equity", "rr.uk", "2026-06-01", "2026-06-02")
    assert out == {"2026-06-01": 10.0}
    assert seen["sym"] == "RR.L"  # base.market resolved to the Yahoo symbol


def test_historical_closes_index_passes_raw_symbol(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        trading, "_yahoo_closes", lambda sym, s, e: seen.setdefault("sym", sym) or {}
    )
    trading.historical_closes("index", "^GSPC", "2026-06-01", "2026-06-02")
    assert seen["sym"] == "^GSPC"  # raw symbol, no resolver


def test_historical_closes_crypto_routes_kraken(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        trading,
        "_kraken_closes",
        lambda pair, since: seen.setdefault("pair", pair) or {"2026-06-01": 60000.0},
    )

    def _no_yahoo(*a, **k):
        raise AssertionError("crypto must not route through Yahoo")

    monkeypatch.setattr(trading, "_yahoo_closes", _no_yahoo)
    out = trading.historical_closes("crypto", "XBTUSD", "2026-06-01", "2026-06-02")
    assert out == {"2026-06-01": 60000.0}
    assert seen["pair"] == "XBTUSD"


def test_historical_closes_prediction_returns_empty():
    assert trading.historical_closes("prediction", "some-market", "2026-06-01", "2026-06-02") == {}


def test_historical_closes_unparseable_equity_returns_empty(monkeypatch):
    monkeypatch.setattr(
        trading, "_yahoo_closes", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    # 'AAPL' has no .market suffix -> _parse_symbol returns None -> {} without fetch
    assert trading.historical_closes("equity", "AAPL", "2026-06-01", "2026-06-02") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k historical_closes -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute 'historical_closes'`

- [ ] **Step 3: Write minimal implementation**

Add to `trading.py` after `fetch_price`:

```python
def historical_closes(
    asset_class: str, instrument: str, start: str, end: str
) -> dict[str, float]:
    """Daily close series for one position over [start, end], routed by asset class.

    equity → Yahoo (base.market resolved); index → Yahoo (raw symbol);
    crypto → Kraken OHLC. Prediction and anything unrecognised → {}. The map feeds
    _snap_close to price a checkpoint at its true crossing date; {} → the caller
    falls back to the current mark price.
    """
    if asset_class == "crypto":
        return _kraken_closes(instrument, start)
    if asset_class == "index":
        return _yahoo_closes(instrument, start, end)
    if asset_class == "equity":
        parsed = _parse_symbol(instrument)
        if parsed is None:
            return {}
        return _yahoo_closes(_yahoo_format_symbol(*parsed), start, end)
    return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k historical_closes -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): historical_closes dispatcher routes series by asset class

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Rewrite `_record_checkpoints` for per-checkpoint pricing (behavior-preserving)

This task adds the `closes` parameter + `price_basis` field and the per-checkpoint historical/current logic, but **both call sites pass `{}`** — so live behavior is unchanged (everything still uses the current mark price, now flagged `"current"`). Task 6 turns on the real fetch.

**Files:**
- Modify: `trading.py` — `_record_checkpoints` (~line 1531), its call in `mark_to_market` (~line 1562), its call in `_mtm_prediction` (~line 1606). Add `_has_new_crossing` just before `_record_checkpoints`.
- Test: `tests/test_checkpoint_backfill.py`

**Interfaces:**
- Consumes: `_snap_close`, `_signal_return`, `PAPER_HORIZONS`, `datetime`/`timedelta`.
- Produces:
  - `_record_checkpoints(p, today_str, price, ret, days_open, closes) -> None` — for each crossed-but-unrecorded horizon, records `{date, price, return, price_basis}`: historical when `_snap_close(closes, entry_date+threshold)` hits, else current.
  - `_has_new_crossing(p: dict, days_open: int) -> bool` — True if any horizon has crossed but isn't recorded yet.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checkpoint_backfill.py`:

```python
def _equity_pos(**over):
    p = {
        "asset_class": "equity",
        "instrument": "aapl.us",
        "ticker": "AAPL",
        "direction": "bullish",
        "entry_price": 100.0,
        "entry_date": "2026-06-01",
        "checkpoints": {},
        "status": "open",
    }
    p.update(over)
    return p


def test_record_checkpoints_uses_historical_when_available():
    p = _equity_pos()
    # 1w crossing date = 2026-06-08; provide that close in the series.
    closes = {"2026-06-08": 110.0}
    trading._record_checkpoints(p, "2026-06-11", 130.0, 0.30, 10, closes)
    cp = p["checkpoints"]["1w"]
    assert cp["price"] == 110.0
    assert cp["date"] == "2026-06-08"
    assert cp["return"] == pytest.approx(0.10)  # bullish 100 -> 110
    assert cp["price_basis"] == "historical"


def test_record_checkpoints_falls_back_to_current_when_missing():
    p = _equity_pos()
    trading._record_checkpoints(p, "2026-06-11", 130.0, 0.30, 10, {})
    cp = p["checkpoints"]["1w"]
    assert cp["price"] == 130.0
    assert cp["date"] == "2026-06-11"
    assert cp["return"] == pytest.approx(0.30)
    assert cp["price_basis"] == "current"


def test_record_checkpoints_bearish_historical_return():
    p = _equity_pos(direction="bearish")
    closes = {"2026-06-08": 90.0}
    trading._record_checkpoints(p, "2026-06-11", 130.0, 0.30, 10, closes)
    cp = p["checkpoints"]["1w"]
    assert cp["return"] == pytest.approx(0.10)  # bearish 100 -> 90 = +10%
    assert cp["price_basis"] == "historical"


def test_record_checkpoints_idempotent_skips_recorded():
    p = _equity_pos(checkpoints={"1w": {"date": "x", "price": 1.0, "return": 0.0}})
    trading._record_checkpoints(p, "2026-06-11", 130.0, 0.30, 10, {"2026-06-08": 110.0})
    assert p["checkpoints"]["1w"]["price"] == 1.0  # untouched


def test_has_new_crossing():
    p = _equity_pos()
    assert trading._has_new_crossing(p, 7) is True
    assert trading._has_new_crossing(p, 6) is False
    p2 = _equity_pos(checkpoints={"1w": {}})
    assert trading._has_new_crossing(p2, 10) is False  # 1w recorded, 2w not yet (14)
    assert trading._has_new_crossing(p2, 14) is True
```

Add `import pytest` to the top of the test file if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k "record_checkpoints or has_new_crossing" -v`
Expected: FAIL — `_has_new_crossing` missing / `_record_checkpoints` takes 5 args not 6.

- [ ] **Step 3: Write the implementation**

Replace `_record_checkpoints` in `trading.py` and add `_has_new_crossing` just before it:

```python
def _has_new_crossing(p: dict, days_open: int) -> bool:
    """True if any horizon has crossed but isn't recorded yet — gates the fetch."""
    return any(
        label not in p["checkpoints"] and days_open >= threshold
        for label, threshold in PAPER_HORIZONS.items()
    )


def _record_checkpoints(
    p: dict,
    today_str: str,
    price: float,
    ret: float,
    days_open: int,
    closes: dict[str, float],
):
    """Record any crossed-but-unrecorded horizon checkpoints (idempotent, one pass).

    Each checkpoint is priced at the close on its true crossing date
    (entry_date + threshold) when available in `closes` (price_basis="historical");
    otherwise it falls back to the current mark price/return (price_basis="current").
    `closes` is {} for prediction and for runs where nothing newly crossed.
    """
    entry = datetime.strptime(p["entry_date"], "%Y-%m-%d").date()
    for label, threshold in PAPER_HORIZONS.items():
        if label in p["checkpoints"] or days_open < threshold:
            continue
        crossing = (entry + timedelta(days=threshold)).strftime("%Y-%m-%d")
        hist = _snap_close(closes, crossing)
        if hist is not None:
            p["checkpoints"][label] = {
                "date": crossing,
                "price": hist,
                "return": _signal_return(p["direction"], p["entry_price"], hist),
                "price_basis": "historical",
            }
        else:
            p["checkpoints"][label] = {
                "date": today_str,
                "price": price,
                "return": ret,
                "price_basis": "current",
            }
```

Update the call site in `mark_to_market` (was `_record_checkpoints(p, today_str, price, ret, days_open)`):

```python
        _record_checkpoints(p, today_str, price, ret, days_open, {})
```

Update the call site in `_mtm_prediction` (same old signature) to:

```python
    _record_checkpoints(p, today_str, price, ret, days_open, {})
```

- [ ] **Step 4: Run the new tests + the full suite**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k "record_checkpoints or has_new_crossing" -v`
Expected: PASS (6 passed)

Run: `python -m pytest -q`
Expected: full suite green (existing prediction/trading tests still pass — they assert `"4w" in checkpoints` and `realized_return`, both unaffected by the additive field).

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): per-checkpoint price_basis + _has_new_crossing gate

Rewrites _record_checkpoints to price each horizon at its true crossing-date
close when supplied (price_basis="historical"), else fall back to the current
mark (price_basis="current"). Both call sites still pass {} — behavior-preserving;
Task 6 wires the real fetch.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Wire `mark_to_market` to fetch the series once per position

**Files:**
- Modify: `trading.py` — `mark_to_market` equity/crypto branch (~line 1559-1562)
- Test: `tests/test_checkpoint_backfill.py`

**Interfaces:**
- Consumes: `historical_closes`, `_has_new_crossing`, `price_position`, `_signal_return`.
- Produces: no new symbol — `mark_to_market` now fetches `historical_closes(asset_class, instrument, entry_date, today_str)` once per position when `_has_new_crossing` is True, and passes the map to `_record_checkpoints`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checkpoint_backfill.py`:

```python
def test_mtm_records_historical_1w_checkpoint(monkeypatch):
    # Entered 2026-06-01, marking 2026-06-11 (10 days open -> 1w crossed).
    monkeypatch.setattr(trading, "price_position", lambda p: 130.0)
    monkeypatch.setattr(
        trading,
        "historical_closes",
        lambda ac, inst, s, e: {"2026-06-08": 110.0},
    )
    book = {"positions": [_equity_pos()]}
    out = trading.mark_to_market(book, "2026-06-11")
    cp = out["positions"][0]["checkpoints"]["1w"]
    assert cp["price"] == 110.0
    assert cp["price_basis"] == "historical"
    assert cp["return"] == pytest.approx(0.10)


def test_mtm_fetch_miss_falls_back_to_current(monkeypatch):
    monkeypatch.setattr(trading, "price_position", lambda p: 130.0)
    monkeypatch.setattr(trading, "historical_closes", lambda ac, inst, s, e: {})
    out = trading.mark_to_market({"positions": [_equity_pos()]}, "2026-06-11")
    cp = out["positions"][0]["checkpoints"]["1w"]
    assert cp["price"] == 130.0
    assert cp["price_basis"] == "current"


def test_mtm_4w_cross_closes_with_historical_realized_return(monkeypatch):
    # Entered 2026-06-01, marking 2026-07-01 (30 days -> 1w/2w/4w all crossed).
    monkeypatch.setattr(trading, "price_position", lambda p: 200.0)
    monkeypatch.setattr(
        trading,
        "historical_closes",
        lambda ac, inst, s, e: {
            "2026-06-08": 110.0,  # 1w
            "2026-06-15": 120.0,  # 2w
            "2026-06-29": 150.0,  # 4w (crossing date 2026-06-29)
        },
    )
    out = trading.mark_to_market({"positions": [_equity_pos()]}, "2026-07-01")
    pos = out["positions"][0]
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "horizon"
    assert pos["realized_return"] == pytest.approx(0.50)  # historical 4w 100->150
    assert pos["checkpoints"]["4w"]["price_basis"] == "historical"


def test_mtm_no_crossing_skips_fetch(monkeypatch):
    # Entered 2026-06-01, marking 2026-06-04 (3 days -> nothing crossed).
    monkeypatch.setattr(trading, "price_position", lambda p: 105.0)

    def _no_fetch(*a, **k):
        raise AssertionError("must not fetch history when nothing crossed")

    monkeypatch.setattr(trading, "historical_closes", _no_fetch)
    out = trading.mark_to_market({"positions": [_equity_pos()]}, "2026-06-04")
    assert out["positions"][0]["checkpoints"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k mtm -v`
Expected: FAIL — `test_mtm_records_historical_1w_checkpoint` records `price_basis="current"` (still passing `{}`); `test_mtm_no_crossing_skips_fetch` passes only by luck. The historical/realized-return assertions fail.

- [ ] **Step 3: Write the implementation**

In `mark_to_market`, replace the equity/crypto block that computes `days_open` and calls `_record_checkpoints`:

```python
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        closes = (
            historical_closes(
                p.get("asset_class", "equity"),
                p["instrument"],
                p["entry_date"],
                today_str,
            )
            if _has_new_crossing(p, days_open)
            else {}
        )
        _record_checkpoints(p, today_str, price, ret, days_open, closes)
```

(Leave the `if PAPER_CLOSE_HORIZON in p["checkpoints"]:` close-trigger block below it unchanged — `realized_return` now reads the historical 4w return automatically.)

- [ ] **Step 4: Run the new tests + the full suite**

Run: `python -m pytest tests/test_checkpoint_backfill.py -k mtm -v`
Expected: PASS (4 passed)

Run: `python -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_checkpoint_backfill.py
git commit -F - <<'EOF'
feat(trading): fetch true crossing-date prices in weekly mark-to-market

mark_to_market now fetches a position's historical close series once (only when
a horizon newly crossed) and prices each checkpoint at its entry_date+threshold
close, falling back to the current mark on any miss. Fixes the checkpoint-backfill
price skew (backlog #1): 1w/2w/4w stats and the 4w realized_return are now anchored
to the true crossing date instead of whenever the weekly run happened to land.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: Final gate + push

**Files:** none (verification only).

- [ ] **Step 1: Run the full pre-push gate**

Run: `ruff check . && ruff format --check . && python -m pytest -q`
Expected: ruff clean, formatter clean, all tests pass. If the formatter reports changes, run `ruff format .`, re-stage, and amend the relevant commit (or add a `style:` commit).

- [ ] **Step 2: Push to main**

```bash
git push origin main
```

Expected: push succeeds; Docker build/deploy triggers as usual.

- [ ] **Step 3: Post-deploy note**

No host config change is required — the feature is always on and fail-safe. The first weekly mark-to-market after deploy will start stamping `price_basis` on newly-crossed checkpoints. Existing checkpoints in the live `book.json` are left as-is (legacy, treated as `"current"`).

---

## Self-Review

**1. Spec coverage:**
- True crossing-date close for equity/index/crypto → Tasks 2/3/4 (fetchers + dispatcher), Task 6 (wiring). ✓
- Snap to last trading day on/before crossing date → Task 1 (`_snap_close`). ✓
- Fallback to current price + `price_basis` flag → Task 5. ✓
- Prediction stays current-price, flagged → Task 5 (`_mtm_prediction` passes `{}`). ✓
- Additive schema, no migration → Task 5 (field added), Task 6 step 3 note. ✓
- One fetch per position, only when crossed → Task 5 (`_has_new_crossing`), Task 6. ✓
- `requests`-only, no `yfinance` → Tasks 2/3 use `requests` + existing helpers. ✓
- Tests parse/transform only in CI; network not exercised → all fetcher tests monkeypatch `requests.get`. ✓
- No reporting change in v1 → no `performance_report` task. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every command shows expected output. ✓

**3. Type consistency:** `_snap_close(closes, target_date) -> float | None`, `_yahoo_closes/_kraken_closes/historical_closes(... ) -> dict[str, float]`, `_record_checkpoints(p, today_str, price, ret, days_open, closes)`, `_has_new_crossing(p, days_open) -> bool` — names and signatures are identical across the tasks that define and call them. `price_basis` values are exactly `"historical"`/`"current"` throughout. ✓
