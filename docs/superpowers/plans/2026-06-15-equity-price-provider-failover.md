# Equity Price Provider Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dead Stooq price feed with a market-routed, multi-provider equity pricer (Alpaca for US, Yahoo for US fallback + UK/DE/FR + the S&P 500 index) so the paper-trading book can price equities again.

**Architecture:** A small `Quote(close, open_, volume)` value object plus a `fetch_quote(base, market)` router that tries providers in a per-market order and returns the first non-`None` result. Two raw-`requests` adapters (`_yahoo_quote`, `_alpaca_quote`) live in `trading.py` alongside the existing Kraken/PolyGram fetchers. The persisted `position["instrument"]` format (`aapl.us` / `rr.uk` / `exv1.de`) is unchanged — it is already a neutral `base.market` symbol — so `book.json` needs no migration. All Stooq functions are deleted.

**Tech Stack:** Python 3.12, `requests`, `pytest`, `ruff`. Yahoo `v8/chart` JSON (keyless, browser UA). Alpaca `v2/stocks/{symbol}/snapshot` (free IEX feed, API key).

---

## Why this design (context for the worker)

- **Root cause:** Stooq removed its `/q/l/` light-quote CSV endpoint (now HTTP 404 for every symbol) and put the rest behind a JavaScript proof-of-work bot wall. A plain `requests.get` can never price through it again. Verified live 2026-06-15.
- **Provider research (2026-06-15):** Among free tiers, **only Yahoo** covers UK/DE/FR equities *and* real indices. Alpaca/Tiingo/Finnhub are US-only and have no index symbols; Twelve Data gates international behind a paid plan; T212's API has no quote endpoint for instruments you don't hold. Hence: Yahoo is mandatory for international + index; Alpaca is the reliable, official US primary with Yahoo as US fallback.
- **Graceful degradation is the existing contract:** every pricer returns `None` on failure and callers skip / render "—", never guessing. Preserve this exactly. UK/DE/FR names have no free backup, so when Yahoo is down they degrade to `None` — same behavior as today.
- **Symbol format:** `position["instrument"]` is `"<base>.<market>"` with market ∈ {`us`,`uk`,`de`,`fr`}. Keep it. Adapters translate the market to their own dialect.

### Provider routing (the contract `fetch_quote` implements)

| market | provider order |
|--------|----------------|
| `us` | `_alpaca_quote` → `_yahoo_quote` |
| `uk` / `de` / `fr` | `_yahoo_quote` only |
| benchmark (S&P 500) | `_yahoo_quote("^GSPC")` → `_alpaca_quote("SPY")` |

### Symbol dialects

| market | neutral | Yahoo | Alpaca |
|--------|---------|-------|--------|
| us | `aapl.us` | `AAPL` | `AAPL` |
| uk | `rr.uk` | `RR.L` | (unsupported → None) |
| de | `exv1.de` | `EXV1.DE` | (unsupported → None) |
| fr | `mc.fr` | `MC.PA` | (unsupported → None) |

**GBp gotcha:** Yahoo prices LSE (`.L`) in pence. Detect `meta.currency == "GBp"` in the chart response and divide close/open by 100. Do **not** hardcode `/100` for all `uk` — read the currency field.

---

## File Structure

- **Modify `trading.py`** — delete `fetch_stooq_price`, `fetch_stooq_volume`, `_stooq_daily_move`; add `Quote`, `_parse_symbol`, `_yahoo_format_symbol`, `_yahoo_quote`, `_alpaca_quote`, `fetch_quote`, `fetch_benchmark`; rename `resolve_stooq_symbol`→`resolve_symbol`; rewire `fetch_price`/`fetch_volume`/`fetch_daily_move`/`fetch_benchmark_level`.
- **Modify `common.py`** — add Alpaca credential constants (mirrors the T212 block).
- **Modify `tests/test_paper.py`** — rename resolver tests; replace Stooq parse tests with Yahoo/Alpaca/`fetch_quote` tests.
- **Modify `tests/test_trading.py`** — update any Stooq-coupled references.
- **Modify `.env.example`, `docker-compose.yml`, `README.md`** — document the Alpaca keys.

No new module → **no Dockerfile / CI `paths` change** (per the project's COPY-allowlist rule).

---

### Task 1: Quote value object, symbol parser, resolver rename

**Files:**
- Modify: `trading.py` (near the top with the other module constants, and at `resolve_stooq_symbol` ~line 779)
- Test: `tests/test_paper.py` (resolver section ~line 19)

- [ ] **Step 1: Write failing tests for `_parse_symbol` and the renamed resolver**

In `tests/test_paper.py`, replace the resolver test block. Each old `resolve_stooq_symbol` call becomes `resolve_symbol`, and add `_parse_symbol` tests:

```python
# ── _parse_symbol ─────────────────────────────────────────────────────────────
def test_parse_symbol_us():
    assert trading._parse_symbol("aapl.us") == ("aapl", "us")


def test_parse_symbol_uk():
    assert trading._parse_symbol("rr.uk") == ("rr", "uk")


def test_parse_symbol_no_market_returns_none():
    assert trading._parse_symbol("garbage") is None


# ── resolve_symbol (renamed from resolve_stooq_symbol; same outputs) ──────────
def test_override_is_authoritative():
    assert trading.resolve_symbol("FOO", CACHE, {"FOO": "foo.us"}) == "foo.us"


def test_exact_t212_us_ticker():
    assert trading.resolve_symbol("AAPL_US_EQ", CACHE, {}) == "aapl.us"


def test_plain_symbol_base_match_prefers_us_listing():
    assert trading.resolve_symbol("SHEL", CACHE, {}) == "shel.us"


def test_lse_two_part_ticker_strips_market_marker():
    assert trading.resolve_symbol("RRl_EQ", CACHE, {}) == "rr.uk"


def test_xetra_eur_resolved_by_isin_country():
    assert trading.resolve_symbol("EXV1d_EQ", CACHE, {}) == "exv1.de"


def test_unknown_currency_returns_none():
    assert trading.resolve_symbol("XYZ_PL_EQ", CACHE, {}) is None


def test_unknown_symbol_returns_none():
    assert trading.resolve_symbol("NOPE", CACHE, {}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_paper.py -k "parse_symbol or resolve_symbol" -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute '_parse_symbol'` / `resolve_symbol`.

- [ ] **Step 3: Add `Quote`, `_parse_symbol`, and rename the resolver**

In `trading.py`, add near the top imports (the file already imports from `typing`; add `NamedTuple` if absent):

```python
from typing import NamedTuple


class Quote(NamedTuple):
    """A single instrument's daily quote. Any field may be None when the provider
    omits it; callers treat a None field as 'unavailable' and skip (never guess)."""

    close: float
    open_: float | None
    volume: float | None


# Neutral instrument symbol is '<base>.<market>' with market in this set.
_MARKETS = {"us", "uk", "de", "fr"}


def _parse_symbol(symbol: str) -> tuple[str, str] | None:
    """Split a neutral instrument symbol ('aapl.us') into (base, market).

    Returns None when the string has no recognised market suffix — callers skip.
    """
    base, _, market = symbol.rpartition(".")
    if not base or market not in _MARKETS:
        return None
    return base, market
```

Rename `resolve_stooq_symbol` to `resolve_symbol` (signature and body unchanged — its `base.market` output is already provider-neutral). Update its docstring's first line to:

```python
def resolve_symbol(ticker: str, cache: dict, overrides: dict) -> str | None:
    """Map a signal ticker to a neutral instrument symbol (e.g. 'aapl.us').
```

- [ ] **Step 4: Update the two internal callers of the renamed resolver**

`resolve_stooq_symbol` is called at `trading.py:898` and `trading.py:1273`. Change both to `resolve_symbol`. Verify with:

Run: `python -m pytest tests/test_paper.py -k "parse_symbol or resolve_symbol" -v`
Expected: PASS (all 12).

Run: `grep -rn "resolve_stooq_symbol" trading.py tests/`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_paper.py
git commit -m "refactor: rename resolve_stooq_symbol -> resolve_symbol; add Quote + _parse_symbol"
```

---

### Task 2: Yahoo adapter (`_yahoo_format_symbol`, `_yahoo_quote`)

**Files:**
- Modify: `trading.py` (add after the `Quote`/`_parse_symbol` block)
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write failing tests for the Yahoo adapter**

Add to `tests/test_paper.py`. These monkeypatch `requests.get` with a fake returning the Yahoo `v8/chart` JSON shape.

```python
# ── _yahoo_format_symbol ──────────────────────────────────────────────────────
def test_yahoo_format_us():
    assert trading._yahoo_format_symbol("aapl", "us") == "AAPL"


def test_yahoo_format_lse():
    assert trading._yahoo_format_symbol("rr", "uk") == "RR.L"


def test_yahoo_format_xetra():
    assert trading._yahoo_format_symbol("exv1", "de") == "EXV1.DE"


def test_yahoo_format_paris():
    assert trading._yahoo_format_symbol("mc", "fr") == "MC.PA"


# ── _yahoo_quote ──────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _yahoo_payload(currency="USD", close=123.45, open_=120.0, volume=1000):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": currency,
                        "regularMarketPrice": close,
                    },
                    "indicators": {
                        "quote": [
                            {
                                "open": [open_],
                                "close": [close],
                                "volume": [volume],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_yahoo_quote_us_parses_fields(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeResp(_yahoo_payload())
    )
    q = trading._yahoo_quote("aapl", "us")
    assert q == trading.Quote(close=123.45, open_=120.0, volume=1000.0)


def test_yahoo_quote_lse_converts_pence_to_pounds(monkeypatch):
    # GBp = pence; 2750 pence -> 27.50, open 2700 -> 27.00
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeResp(_yahoo_payload(currency="GBp", close=2750.0, open_=2700.0)),
    )
    q = trading._yahoo_quote("rr", "uk")
    assert q.close == 27.5
    assert q.open_ == 27.0


def test_yahoo_quote_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeResp({}, status=429)
    )
    assert trading._yahoo_quote("aapl", "us") is None


def test_yahoo_quote_nonpositive_close_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeResp(_yahoo_payload(close=0.0)),
    )
    assert trading._yahoo_quote("aapl", "us") is None


def test_yahoo_quote_empty_result_returns_none(monkeypatch):
    monkeypatch.setattr(
        trading.requests,
        "get",
        lambda *a, **k: _FakeResp({"chart": {"result": None, "error": "x"}}),
    )
    assert trading._yahoo_quote("aapl", "us") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_paper.py -k "yahoo" -v`
Expected: FAIL with `AttributeError: ... has no attribute '_yahoo_format_symbol'`.

- [ ] **Step 3: Implement the Yahoo adapter**

Add to `trading.py`:

```python
# Yahoo market suffixes. US is bare; the rest carry an exchange suffix.
_YAHOO_SUFFIX = {"us": "", "uk": ".L", "de": ".DE", "fr": ".PA"}

# A browser UA is enough for the keyless v8/chart endpoint (no crumb/cookie needed).
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _yahoo_format_symbol(base: str, market: str) -> str:
    """Translate a neutral (base, market) to a Yahoo ticker ('rr','uk' -> 'RR.L')."""
    return f"{base.upper()}{_YAHOO_SUFFIX.get(market, '')}"


def _yahoo_fetch(yahoo_symbol: str) -> Quote | None:
    """Fetch a daily Quote for an already-formatted Yahoo symbol via v8/chart.

    Handles the GBp (pence) -> GBP (pounds) /100 conversion via the response's
    currency field. Returns None on any network/parse failure or non-positive
    close — callers treat None as 'could not price' and skip.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "range": "1d"},
            headers=_YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Yahoo fetch failed for {yahoo_symbol}: {e}")
        return None
    result = (data.get("chart") or {}).get("result")
    if not result:
        log.warning(f"Yahoo returned no result for {yahoo_symbol}")
        return None
    node = result[0]
    meta = node.get("meta") or {}
    try:
        quote = node["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return None

    def _last(key):  # last non-null entry in the series, else None
        seq = quote.get(key) or []
        for v in reversed(seq):
            if v is not None:
                return float(v)
        return None

    close = _last("close")
    if close is None:
        close = meta.get("regularMarketPrice")
        close = float(close) if close is not None else None
    if close is None or close <= 0:
        log.warning(f"Yahoo returned no usable close for {yahoo_symbol}")
        return None
    open_ = _last("open")
    volume = _last("volume")
    # GBp is pence; convert price fields (not volume) to the major unit.
    if meta.get("currency") == "GBp":
        close /= 100.0
        if open_ is not None:
            open_ /= 100.0
    return Quote(close=close, open_=open_, volume=volume)


def _yahoo_quote(base: str, market: str) -> Quote | None:
    """Daily Quote for a neutral (base, market) from Yahoo."""
    return _yahoo_fetch(_yahoo_format_symbol(base, market))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_paper.py -k "yahoo" -v`
Expected: PASS (9).

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_paper.py
git commit -m "feat: add Yahoo v8/chart equity quote adapter (with GBp/100 handling)"
```

---

### Task 3: Alpaca adapter + credentials

**Files:**
- Modify: `common.py` (after the T212 block ~line 74)
- Modify: `trading.py` (imports from common; add `_alpaca_quote`)
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write failing tests for the Alpaca adapter**

Add to `tests/test_paper.py` (reuses `_FakeResp` from Task 2):

```python
def _alpaca_snapshot(close=200.0, open_=198.0, volume=5000):
    return {
        "dailyBar": {"o": open_, "h": 0, "l": 0, "c": close, "v": volume},
        "latestTrade": {"p": close},
        "prevDailyBar": {"o": 0, "h": 0, "l": 0, "c": close, "v": volume},
    }


def test_alpaca_quote_parses_daily_bar(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "k")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "s")
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeResp(_alpaca_snapshot())
    )
    q = trading._alpaca_quote("AAPL")
    assert q == trading.Quote(close=200.0, open_=198.0, volume=5000.0)


def test_alpaca_quote_no_keys_returns_none(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "")
    # Must not even attempt a request when unconfigured.
    def _boom(*a, **k):
        raise AssertionError("should not call the network without keys")

    monkeypatch.setattr(trading.requests, "get", _boom)
    assert trading._alpaca_quote("AAPL") is None


def test_alpaca_quote_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(trading.common, "ALPACA_API_KEY_ID", "k")
    monkeypatch.setattr(trading.common, "ALPACA_API_SECRET", "s")
    monkeypatch.setattr(
        trading.requests, "get", lambda *a, **k: _FakeResp({}, status=429)
    )
    assert trading._alpaca_quote("AAPL") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_paper.py -k "alpaca" -v`
Expected: FAIL (`AttributeError` on `_alpaca_quote` / `ALPACA_API_KEY_ID`).

- [ ] **Step 3: Add Alpaca credentials to `common.py`**

After the T212 block in `common.py`:

```python
# Alpaca market-data credentials (optional, like T212). Free signup gives a
# paper account with full Basic/IEX data access — no funding required.
ALPACA_API_KEY_ID = os.environ.get("APCA_API_KEY_ID", "").strip()
ALPACA_API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "").strip()
ALPACA_DATA_URL = os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets").strip()
```

- [ ] **Step 4: Implement `_alpaca_quote` in `trading.py`**

Ensure `trading.py` imports the module as `common` (it already imports names from common; confirm `import common` is present — if only `from common import (...)` is used, add `import common` so tests can monkeypatch `trading.common.ALPACA_*`). Then add:

```python
def _alpaca_quote(symbol: str) -> Quote | None:
    """Daily Quote for a US symbol from Alpaca's snapshot endpoint (free IEX feed).

    Returns None when Alpaca is unconfigured or on any network/parse failure /
    non-positive close — callers skip (mirrors the other pricers). US-only: the
    free feed has no international listings, so the router only routes US here.
    """
    if not common.ALPACA_API_KEY_ID or not common.ALPACA_API_SECRET:
        return None
    url = f"{common.ALPACA_DATA_URL}/v2/stocks/{symbol}/snapshot"
    headers = {
        "APCA-API-KEY-ID": common.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": common.ALPACA_API_SECRET,
    }
    try:
        resp = requests.get(url, headers=headers, params={"feed": "iex"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Alpaca fetch failed for {symbol}: {e}")
        return None
    bar = data.get("dailyBar") or data.get("prevDailyBar") or {}
    try:
        close = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        log.warning(f"Alpaca returned no usable close for {symbol}")
        return None
    if close <= 0:
        return None

    def _opt(key):
        v = bar.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return Quote(close=close, open_=_opt("o"), volume=_opt("v"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_paper.py -k "alpaca" -v`
Expected: PASS (3).

- [ ] **Step 6: Commit**

```bash
git add common.py trading.py tests/test_paper.py
git commit -m "feat: add Alpaca snapshot quote adapter + credentials"
```

---

### Task 4: `fetch_quote` router + `fetch_benchmark`

**Files:**
- Modify: `trading.py`
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write failing tests for routing + failover**

Add to `tests/test_paper.py`:

```python
def test_fetch_quote_us_prefers_alpaca(monkeypatch):
    calls = []
    monkeypatch.setattr(
        trading, "_alpaca_quote",
        lambda s: calls.append(("alpaca", s)) or trading.Quote(1.0, None, None),
    )
    monkeypatch.setattr(
        trading, "_yahoo_quote",
        lambda b, m: calls.append(("yahoo", b, m)) or trading.Quote(9.0, None, None),
    )
    q = trading.fetch_quote("aapl", "us")
    assert q.close == 1.0
    assert calls == [("alpaca", "AAPL")]  # Yahoo not tried when Alpaca succeeds


def test_fetch_quote_us_falls_back_to_yahoo(monkeypatch):
    monkeypatch.setattr(trading, "_alpaca_quote", lambda s: None)
    monkeypatch.setattr(
        trading, "_yahoo_quote", lambda b, m: trading.Quote(9.0, None, None)
    )
    assert trading.fetch_quote("aapl", "us").close == 9.0


def test_fetch_quote_uk_uses_yahoo_only(monkeypatch):
    def _boom(s):
        raise AssertionError("Alpaca has no UK data; must not be called")

    monkeypatch.setattr(trading, "_alpaca_quote", _boom)
    monkeypatch.setattr(
        trading, "_yahoo_quote", lambda b, m: trading.Quote(27.5, None, None)
    )
    assert trading.fetch_quote("rr", "uk").close == 27.5


def test_fetch_quote_unparseable_symbol_returns_none():
    assert trading.fetch_quote("garbage", "zz") is None


def test_fetch_benchmark_prefers_yahoo_gspc(monkeypatch):
    seen = []
    monkeypatch.setattr(
        trading, "_yahoo_fetch",
        lambda s: seen.append(s) or trading.Quote(5000.0, None, None),
    )
    monkeypatch.setattr(trading, "_alpaca_quote", lambda s: trading.Quote(500.0, None, None))
    assert trading.fetch_benchmark() == 5000.0
    assert seen == ["^GSPC"]


def test_fetch_benchmark_falls_back_to_spy(monkeypatch):
    monkeypatch.setattr(trading, "_yahoo_fetch", lambda s: None)
    monkeypatch.setattr(
        trading, "_alpaca_quote",
        lambda s: trading.Quote(500.0, None, None) if s == "SPY" else None,
    )
    assert trading.fetch_benchmark() == 500.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_paper.py -k "fetch_quote or benchmark" -v`
Expected: FAIL (`AttributeError` on `fetch_quote` / `fetch_benchmark`).

- [ ] **Step 3: Implement the router and benchmark**

Add to `trading.py`:

```python
# Per-market provider order. Each entry is a 0-arg-after-symbol callable chain
# built in fetch_quote; US gets Alpaca-then-Yahoo, the rest are Yahoo-only.
def fetch_quote(base_or_symbol: str, market: str | None = None) -> Quote | None:
    """Daily Quote for an instrument, routed by market with failover.

    Accepts either (base, market) or a single neutral symbol ('aapl.us'). US
    routes Alpaca -> Yahoo; UK/DE/FR route Yahoo only. Returns the first non-None
    provider result, or None when nothing prices it (callers skip, never guess).
    """
    if market is None:
        parsed = _parse_symbol(base_or_symbol)
        if parsed is None:
            return None
        base, market = parsed
    else:
        base = base_or_symbol
    if market not in _MARKETS:
        return None
    if market == "us":
        return _alpaca_quote(base.upper()) or _yahoo_quote(base, market)
    return _yahoo_quote(base, market)


def fetch_benchmark() -> float | None:
    """S&P 500 level: Yahoo ^GSPC (true index) -> Alpaca SPY ETF (degraded proxy).
    None when neither prices — caller treats as benchmark unavailable."""
    q = _yahoo_fetch("^GSPC") or _alpaca_quote("SPY")
    return q.close if q else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_paper.py -k "fetch_quote or benchmark" -v`
Expected: PASS (6).

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_paper.py
git commit -m "feat: add market-routed fetch_quote with Alpaca->Yahoo failover + S&P benchmark"
```

---

### Task 5: Rewire the public pricers; delete Stooq

**Files:**
- Modify: `trading.py` — `fetch_price` (~671), `fetch_volume` (~313), `fetch_daily_move` (~210), `fetch_benchmark_level` (~322); delete `fetch_stooq_price`, `fetch_stooq_volume`, `_stooq_daily_move`.
- Test: `tests/test_paper.py`, `tests/test_trading.py`

- [ ] **Step 1: Write failing tests for the rewired wrappers**

Add to `tests/test_paper.py`:

```python
def test_fetch_price_equity_uses_quote(monkeypatch):
    monkeypatch.setattr(trading, "fetch_quote", lambda *a: trading.Quote(50.0, 48.0, 10.0))
    assert trading.fetch_price("equity", "aapl.us") == 50.0


def test_fetch_price_equity_none_when_unpriced(monkeypatch):
    monkeypatch.setattr(trading, "fetch_quote", lambda *a: None)
    assert trading.fetch_price("equity", "aapl.us") is None


def test_fetch_volume_equity_uses_quote(monkeypatch):
    monkeypatch.setattr(trading, "fetch_quote", lambda *a: trading.Quote(50.0, 48.0, 10.0))
    assert trading.fetch_volume("equity", "aapl.us") == 10.0


def test_fetch_daily_move_equity_from_quote(monkeypatch):
    # open 48 -> close 50 = +4.17%
    monkeypatch.setattr(trading, "fetch_quote", lambda *a: trading.Quote(50.0, 48.0, 10.0))
    assert trading.fetch_daily_move("equity", "aapl.us") == 4.17


def test_fetch_daily_move_equity_none_without_open(monkeypatch):
    monkeypatch.setattr(trading, "fetch_quote", lambda *a: trading.Quote(50.0, None, 10.0))
    assert trading.fetch_daily_move("equity", "aapl.us") is None


def test_fetch_benchmark_level_equity_delegates(monkeypatch):
    monkeypatch.setattr(trading, "fetch_benchmark", lambda: 5000.0)
    assert trading.fetch_benchmark_level("equity") == 5000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_paper.py -k "fetch_price or fetch_volume or daily_move or benchmark_level" -v`
Expected: FAIL — the wrappers still call deleted Stooq functions / wrong signature.

- [ ] **Step 3: Rewire the four wrappers and delete Stooq functions**

In `trading.py`:

Replace `fetch_price` equity branch:
```python
def fetch_price(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument to market via the pricer for its asset class.

    Equity → multi-provider (Alpaca/Yahoo), crypto → Kraken. None on any pricing
    failure — callers skip, never guess.
    """
    if asset_class == "crypto":
        return fetch_kraken_price(instrument)
    q = fetch_quote(instrument)
    return q.close if q else None
```

Replace `fetch_volume` equity branch (last line):
```python
    q = fetch_quote(instrument)
    return q.volume if q else None
```

Replace `fetch_daily_move` equity branch — delete the `return _stooq_daily_move(instrument)` line and use:
```python
    if asset_class == "crypto":
        return _kraken_daily_move(instrument)
    q = fetch_quote(instrument)
    if q is None or q.open_ is None or q.open_ <= 0:
        return None
    return round((q.close - q.open_) / q.open_ * 100, 2)
```

Replace `fetch_benchmark_level` equity branch:
```python
    if asset_class == "equity":
        return fetch_benchmark()
```

Delete `fetch_stooq_price`, `fetch_stooq_volume`, and `_stooq_daily_move` entirely, plus the now-unused `_STOOQ_SUFFIX` / `_STOOQ_EUR_BY_ISIN` / `_STOOQ_MARKET_MARKER` only if nothing else references them — **check first**: `resolve_symbol` still uses all three. Keep them (they map currency→market, which is still correct). Do **not** delete them.

- [ ] **Step 4: Purge remaining Stooq references in tests**

Run: `grep -rn "stooq\|_STOOQ\|fetch_stooq" tests/`
For each hit in `tests/test_paper.py` / `tests/test_trading.py`, delete or rewrite the test to target `fetch_quote` / the adapters (the parse-level Stooq tests from Task 1/2 already replace them). The currency→market maps `_STOOQ_*` may still be referenced by resolver tests — those are fine; leave them.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS, no errors, no remaining `fetch_stooq*` references.

- [ ] **Step 6: Commit**

```bash
git add trading.py tests/
git commit -m "feat: route equity pricing through fetch_quote; remove dead Stooq feed"
```

---

### Task 6: Config + docs

**Files:**
- Modify: `.env.example`, `docker-compose.yml`, `README.md`

- [ ] **Step 1: Document the Alpaca keys in `.env.example`**

Add after the T212 block:

```bash
# Alpaca market-data API keys (free). Primary price source for US equities, with
# Yahoo as fallback and the source for UK/DE/FR + the S&P 500 index. Free signup
# at https://alpaca.markets gives a paper account with Basic/IEX data — no funding
# required. Leave unset to run Yahoo-only (US names then lose their fallback).
# APCA_API_KEY_ID=
# APCA_API_SECRET_KEY=
# APCA_DATA_URL=https://data.alpaca.markets
```

- [ ] **Step 2: Pass the keys into the container in `docker-compose.yml`**

Find the `environment:` block that forwards `T212_API_KEY_ID` etc. and add the three Alpaca vars in the same `${VAR}` passthrough style:

```yaml
      - APCA_API_KEY_ID=${APCA_API_KEY_ID:-}
      - APCA_API_SECRET_KEY=${APCA_API_SECRET_KEY:-}
      - APCA_DATA_URL=${APCA_DATA_URL:-https://data.alpaca.markets}
```

- [ ] **Step 3: Update `README.md`**

Find the section describing the price source (currently mentions Stooq). Replace the Stooq description with: equities priced via Alpaca (US, primary) + Yahoo (US fallback + UK/DE/FR), S&P 500 via Yahoo `^GSPC` with Alpaca `SPY` fallback; note Stooq was removed 2026-06-15 after it took its free CSV API down.

- [ ] **Step 4: Commit**

```bash
git add .env.example docker-compose.yml README.md
git commit -m "docs: document Alpaca keys; note Stooq removal"
```

---

### Task 7: Final verification gate

- [ ] **Step 1: Run the full pre-push gate** (per project rule: ruff check + ruff format --check + pytest, not pytest alone)

Run:
```bash
python -m ruff check . && python -m ruff format --check . && python -m pytest -q
```
Expected: ruff clean, format clean, all tests pass. If `ruff format` reflows files, `git add` them (CI fails on unstaged reformatting).

- [ ] **Step 2: Live smoke test (optional, requires network)**

Run (PowerShell tool — Bash errors "stdin is not a tty" for python here):
```python
import trading
print("US  :", trading.fetch_quote("aapl.us"))
print("LSE :", trading.fetch_quote("shel.uk"))
print("SPX :", trading.fetch_benchmark())
```
Expected: three non-None results (US close, LSE close already /100 into pounds, S&P level). A `None` for the LSE name without Alpaca keys is fine; the US name should price via Yahoo even without keys.

- [ ] **Step 3: Final commit if anything changed**

```bash
git add -A && git commit -m "chore: verification gate green for price-provider failover"
```

---

## Self-Review

- **Spec coverage:** seam (Task 1) ✓, Yahoo adapter incl. GBp (Task 2) ✓, Alpaca adapter + keys (Task 3) ✓, market routing + failover + benchmark (Task 4) ✓, rewire + delete Stooq (Task 5) ✓, config/docs (Task 6) ✓, verification (Task 7) ✓. Book migration: not needed — `instrument` format unchanged (documented above).
- **Type consistency:** `Quote(close, open_, volume)` used identically across all tasks; `fetch_quote(base, market|symbol)`, `_yahoo_quote(base, market)`, `_yahoo_fetch(yahoo_symbol)`, `_alpaca_quote(symbol)`, `fetch_benchmark() -> float|None`, `resolve_symbol` — all match between definition and use.
- **No placeholders:** every code step shows complete code; commands have expected output.
- **Open verification during execution:** confirm `import common` exists in `trading.py` (Task 3 Step 4) so `trading.common.ALPACA_*` is monkeypatchable; confirm the `docker-compose.yml` env block style before editing (Task 6 Step 2).
