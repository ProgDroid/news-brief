# Phase 5 — Volume Monitor + New Telegram Commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the hourly `monitor` cron mode (cross-asset volume-anomaly alerts) and four new Telegram commands (`/watch`, `/unwatch`, `/positions`, `/performance`), completing the multi-asset trading build.

**Architecture:** Volume parsers reuse the existing Stooq CSV and Kraken Ticker calls (volume is already in those responses); a trailing-mean baseline in `volume-history.json` drives a ratio+floor+warm-up anomaly rule with a per-instrument cooldown. A cross-asset `watchlist.json` feeds the monitor alongside open positions. Monitor logic and watchlist accessors live in `trading.py`; the `mode_monitor` wrapper and command handlers live in `brief.py`; `/performance` reuses `validation.performance_report`.

**Tech Stack:** Python 3, `requests`, `pytest` (env-gated `NEWSBRIEF_DATA_DIR`), Docker Compose host cron.

**Spec:** `docs/superpowers/specs/2026-06-14-phase5-monitor-commands-design.md`

**Conventions to follow (verified in the codebase):**
- All network fetchers return `None` on any failure and log a warning — callers skip, never guess (mirrors `fetch_stooq_price` / `fetch_kraken_price`).
- Persisted state uses `_write_json_atomic` / `_load_json_or` (from `common.py`); both already imported into `trading.py`.
- Run tests via **PowerShell** (the Bash tool errors `stdin is not a tty` for python here): `python -m pytest -q`.
- The full pre-push gate is `ruff check . ; ruff format --check . ; python -m pytest -q` — stage every reformatted file or CI fails.
- Make git commits through the **Bash tool** (PowerShell prepends a UTF-8 BOM to the commit subject).

---

### Task 1: Volume-monitor config knobs (`common.py`)

**Files:**
- Modify: `common.py` (after the `GATE_*` block, ~line 83)
- Test: `tests/test_monitor.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_monitor.py`:

```python
"""Phase 5: volume monitor (parsers, baseline, anomaly, cooldown, watchlist) + commands."""

import common


def test_vol_config_defaults_present():
    assert common.VOL_SPIKE_MULT == 2.5
    assert common.VOL_TRAILING_N == 20
    assert common.VOL_MIN_SAMPLES == 5
    assert common.VOL_ALERT_COOLDOWN_HRS == 12.0
    assert common.VOL_FLOOR_EQUITY == 0.0
    assert common.VOL_FLOOR_CRYPTO == 0.0
    assert common.VOL_FLOOR_PREDICTION == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitor.py::test_vol_config_defaults_present -v`
Expected: FAIL with `AttributeError: module 'common' has no attribute 'VOL_SPIKE_MULT'`

- [ ] **Step 3: Add the constants**

In `common.py`, immediately after the `GATE_SUSTAINED_EVALS = ...` line, add:

```python
# ── Volume monitor (Phase 5) ──────────────────────────────────────────────────
# Anomaly = current-period volume / trailing-mean >= VOL_SPIKE_MULT, gated by an
# optional absolute per-asset floor and a minimum trailing-sample warm-up. A
# per-instrument cooldown suppresses re-alerting (daily equity volume fires once;
# intraday crypto can re-fire after the window).
VOL_SPIKE_MULT = float(os.environ.get("VOL_SPIKE_MULT", "2.5"))
VOL_TRAILING_N = int(os.environ.get("VOL_TRAILING_N", "20"))
VOL_MIN_SAMPLES = int(os.environ.get("VOL_MIN_SAMPLES", "5"))
VOL_ALERT_COOLDOWN_HRS = float(os.environ.get("VOL_ALERT_COOLDOWN_HRS", "12"))
VOL_FLOOR_EQUITY = float(os.environ.get("VOL_FLOOR_EQUITY", "0"))
VOL_FLOOR_CRYPTO = float(os.environ.get("VOL_FLOOR_CRYPTO", "0"))
VOL_FLOOR_PREDICTION = float(os.environ.get("VOL_FLOOR_PREDICTION", "0"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitor.py::test_vol_config_defaults_present -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add common.py tests/test_monitor.py
git commit -m "feat: volume-monitor config knobs (VOL_*)"
```

---

### Task 2: Per-asset volume parsers (`trading.py`)

Volume is already present in the existing calls: Stooq CSV column 7 (`...,Close,Volume`), Kraken `Ticker` result `v[1]` (last-24h). Prediction volume is read from market detail (it lives on the market object in the Polymarket mirror, not in the price series); `None` if absent — the graceful-degradation path from the spec.

**Files:**
- Modify: `trading.py` (add near `fetch_kraken_price`, ~line 150)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
import trading


class _FakeResp:
    def __init__(self, *, text=None, payload=None):
        self._text = text
        self._payload = payload

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_stooq_volume_parses_column_7(monkeypatch):
    csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-06-13,22:00:02,1,2,0.5,1.5,123456\n"
    monkeypatch.setattr(trading.requests, "get", lambda url, timeout=None: _FakeResp(text=csv))
    assert trading.fetch_stooq_volume("aapl.us") == 123456.0


def test_stooq_volume_nd_returns_none(monkeypatch):
    csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\nFOO.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    monkeypatch.setattr(trading.requests, "get", lambda url, timeout=None: _FakeResp(text=csv))
    assert trading.fetch_stooq_volume("foo.us") is None


def test_kraken_volume_parses_24h(monkeypatch):
    payload = {"error": [], "result": {"XXBTZUSD": {"v": ["10.0", "250.5"]}}}
    monkeypatch.setattr(trading.requests, "get", lambda url, timeout=None: _FakeResp(payload=payload))
    assert trading.fetch_kraken_volume("XBTUSD") == 250.5


def test_kraken_volume_error_returns_none(monkeypatch):
    payload = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    monkeypatch.setattr(trading.requests, "get", lambda url, timeout=None: _FakeResp(payload=payload))
    assert trading.fetch_kraken_volume("NOPEUSD") is None


def test_pg_volume_reads_market_field(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"volume24hr": "9999.5"})
    assert trading.fetch_pg_volume("0xabc") == 9999.5


def test_pg_volume_missing_field_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"question": "no volume here"})
    assert trading.fetch_pg_volume("0xabc") is None


def test_pg_volume_unfetchable_market_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert trading.fetch_pg_volume("0xabc") is None


def test_fetch_volume_dispatches_by_asset_class(monkeypatch):
    monkeypatch.setattr(trading, "fetch_stooq_volume", lambda s: 1.0)
    monkeypatch.setattr(trading, "fetch_kraken_volume", lambda p: 2.0)
    monkeypatch.setattr(trading, "fetch_pg_volume", lambda m: 3.0)
    assert trading.fetch_volume("equity", "aapl.us") == 1.0
    assert trading.fetch_volume("crypto", "XBTUSD") == 2.0
    assert trading.fetch_volume("prediction", "0xabc") == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -k volume -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute 'fetch_stooq_volume'`

- [ ] **Step 3: Implement the parsers**

In `trading.py`, after `fetch_kraken_price` (~line 149), add:

```python
def fetch_stooq_volume(stooq_symbol: str) -> float | None:
    """Latest daily Volume for a Stooq symbol (column 7 of the same CSV the pricer
    uses). None on network error / 'N/D' — caller skips (mirrors fetch_stooq_price)."""
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq volume fetch failed for {stooq_symbol}: {e}")
        return None
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")  # Symbol,Date,Time,Open,High,Low,Close,Volume
    if len(cols) < 8 or cols[7] in ("N/D", ""):
        return None
    try:
        vol = float(cols[7])
    except ValueError:
        return None
    return vol if vol >= 0 else None


def fetch_kraken_volume(pair: str) -> float | None:
    """Last-24h traded volume for a Kraken pair (Ticker 'v' = [today, last 24h]).
    None on error / garbled result — caller skips (mirrors fetch_kraken_price)."""
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken volume fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        return None
    result = data.get("result") or {}
    if not result:
        return None
    entry = next(iter(result.values()))
    try:
        vol = float(entry["v"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return vol if vol >= 0 else None


def fetch_pg_volume(market_id: str) -> float | None:
    """24h volume for a PolyGram market, read from market detail (volume lives on
    the market object in the Polymarket mirror, not in the price series). None if
    the market is unfetchable or exposes no volume field — caller skips. This is
    the spec's graceful-degradation path for prediction volume."""
    m = polygram_market(market_id)
    if m is None:
        return None
    for field in ("volume24hr", "volume24Hr", "volume"):
        v = m.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def fetch_volume(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument's volume via the fetcher for its asset class."""
    if asset_class == "crypto":
        return fetch_kraken_volume(instrument)
    if asset_class == "prediction":
        return fetch_pg_volume(instrument)
    return fetch_stooq_volume(instrument)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -k volume -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_monitor.py
git commit -m "feat: per-asset volume parsers (Stooq/Kraken/PolyGram)"
```

---

### Task 3: Baseline, anomaly rule, cooldown & formatting (`trading.py`)

Pure helpers (no I/O) so they unit-test cleanly. The anomaly compares `current` against the mean of the **prior** samples (before the current is appended); warm-up suppresses cold starts; consecutive-duplicate dedup keeps daily equity volume from filling the window with one repeated value.

**Files:**
- Modify: `trading.py` (add near the lifecycle helpers, after `_signal_return`, ~line 705)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
from datetime import datetime, timezone, timedelta


def test_anomaly_fires_above_multiplier():
    prior = [100, 100, 100, 100, 100]  # mean 100, >= VOL_MIN_SAMPLES
    is_spike, ratio = trading._volume_anomaly(prior, 300.0, floor=0.0)
    assert is_spike is True
    assert ratio == 3.0


def test_anomaly_below_multiplier_is_quiet():
    prior = [100, 100, 100, 100, 100]
    is_spike, _ = trading._volume_anomaly(prior, 200.0, floor=0.0)  # 2.0 < 2.5
    assert is_spike is False


def test_anomaly_warmup_suppresses_when_too_few_samples():
    prior = [100, 100]  # < VOL_MIN_SAMPLES (5)
    is_spike, _ = trading._volume_anomaly(prior, 9999.0, floor=0.0)
    assert is_spike is False


def test_anomaly_floor_suppresses_thin_instrument():
    prior = [1, 1, 1, 1, 1]
    is_spike, _ = trading._volume_anomaly(prior, 5.0, floor=100.0)  # 5x but under floor
    assert is_spike is False


def test_append_sample_dedups_consecutive_duplicates():
    assert trading._append_sample([100, 200], 200.0) == [100, 200]
    assert trading._append_sample([100, 200], 300.0) == [100, 200, 300]


def test_append_sample_caps_at_trailing_n():
    big = list(range(common.VOL_TRAILING_N + 5))
    out = trading._append_sample(big, 999.0)
    assert len(out) == common.VOL_TRAILING_N
    assert out[-1] == 999.0


def test_cooldown_active_within_window():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    assert trading._in_cooldown(recent, now) is True


def test_cooldown_expired_after_window():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=common.VOL_ALERT_COOLDOWN_HRS + 1)).isoformat()
    assert trading._in_cooldown(old, now) is False


def test_cooldown_none_is_not_active():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    assert trading._in_cooldown(None, now) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -k "anomaly or append or cooldown" -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute '_volume_anomaly'`

- [ ] **Step 3: Implement the helpers**

In `trading.py`, add the new config names to the `from common import (...)` block (after `HAIRCUT_BPS_PREDICTION`):

```python
    VOL_SPIKE_MULT,
    VOL_TRAILING_N,
    VOL_MIN_SAMPLES,
    VOL_ALERT_COOLDOWN_HRS,
    VOL_FLOOR_EQUITY,
    VOL_FLOOR_CRYPTO,
    VOL_FLOOR_PREDICTION,
```

Then add the helpers after `_signal_return` (~line 705):

```python
def _volume_anomaly(prior: list, current: float, floor: float) -> tuple[bool, float]:
    """(is_spike, ratio) for current volume vs the mean of prior samples.

    Pure. Warm-up (need >= VOL_MIN_SAMPLES prior samples), an absolute floor, and a
    positive baseline all gate the ratio test, so a cold start or a thin instrument
    never alerts.
    """
    if len(prior) < VOL_MIN_SAMPLES or current < floor:
        return (False, 0.0)
    baseline = sum(prior) / len(prior)
    if baseline <= 0:
        return (False, 0.0)
    ratio = current / baseline
    return (ratio >= VOL_SPIKE_MULT, ratio)


def _append_sample(samples: list, current: float) -> list:
    """Append current to the trailing window, deduping a consecutive duplicate
    (daily-resolution equity volume is identical all day → one sample/day, not 24),
    capped at VOL_TRAILING_N most-recent samples."""
    if samples and samples[-1] == current:
        return samples
    return (samples + [current])[-VOL_TRAILING_N:]


def _in_cooldown(last_alert_ts: str | None, now: datetime) -> bool:
    """True if the last alert for this instrument is within VOL_ALERT_COOLDOWN_HRS."""
    if not last_alert_ts:
        return False
    try:
        last = datetime.fromisoformat(last_alert_ts)
    except ValueError:
        return False
    return (now - last) < timedelta(hours=VOL_ALERT_COOLDOWN_HRS)


def _floor_for(asset_class: str) -> float:
    return {
        "equity": VOL_FLOOR_EQUITY,
        "crypto": VOL_FLOOR_CRYPTO,
        "prediction": VOL_FLOOR_PREDICTION,
    }.get(asset_class, 0.0)


def _fmt_vol(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


def _format_alert(
    asset_class: str, instrument: str, current: float, baseline: float, ratio: float
) -> str:
    return (
        f"📈 <b>{asset_class}</b> {instrument}: {ratio:.1f}× avg volume "
        f"({_fmt_vol(current)} vs {_fmt_vol(baseline)})"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -k "anomaly or append or cooldown" -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_monitor.py
git commit -m "feat: volume baseline, anomaly rule, cooldown helpers"
```

---

### Task 4: Cross-asset watchlist storage & resolution (`trading.py`)

One `watchlist.json` holds equity/crypto/prediction entries. `resolve_watch_entry` infers the asset class (crypto → equity; prediction is never inferred and needs an explicit class + market id) and resolves the venue instrument via the existing resolvers.

**Files:**
- Modify: `trading.py` (constants near line 36; helpers near the resolvers / `load_book`)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
def test_watchlist_roundtrip(tmp_path, monkeypatch):
    f = tmp_path / "watchlist.json"
    monkeypatch.setattr(trading, "WATCHLIST_FILE", f)
    assert trading.load_watchlist() == {"items": []}
    trading.save_watchlist({"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]})
    assert trading.load_watchlist()["items"][0]["instrument"] == "XBTUSD"


def test_resolve_watch_infers_crypto():
    entry = trading.resolve_watch_entry("BTC")
    assert entry == {"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}


def test_resolve_watch_infers_equity_when_not_crypto(monkeypatch):
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: "shel.uk")
    entry = trading.resolve_watch_entry("SHEL")
    assert entry == {"raw": "SHEL", "asset_class": "equity", "instrument": "shel.uk"}


def test_resolve_watch_explicit_equity_skips_crypto(monkeypatch):
    # 'BTC' is a known crypto symbol, but an explicit equity class must not infer crypto.
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: "btc.us")
    entry = trading.resolve_watch_entry("BTC", asset_class="equity")
    assert entry["asset_class"] == "equity"
    assert entry["instrument"] == "btc.us"


def test_resolve_watch_prediction_validates_market(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"question": "x"})
    entry = trading.resolve_watch_entry("0xabc", asset_class="prediction")
    assert entry == {"raw": "0xabc", "asset_class": "prediction", "instrument": "0xabc"}


def test_resolve_watch_prediction_bad_market_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "polygram_market", lambda mid: None)
    assert trading.resolve_watch_entry("0xbad", asset_class="prediction") is None


def test_resolve_watch_unresolvable_returns_none(monkeypatch):
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: None)
    assert trading.resolve_watch_entry("NOTATHING") is None


def test_watched_instruments_unions_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    trading.save_watchlist({"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]})
    monkeypatch.setattr(
        trading,
        "load_book",
        lambda: {"positions": [
            {"status": "open", "asset_class": "crypto", "instrument": "XBTUSD"},  # dup of watchlist
            {"status": "open", "asset_class": "equity", "instrument": "shel.uk"},
            {"status": "closed", "asset_class": "equity", "instrument": "bp.uk"},  # ignored
        ]},
    )
    watched = trading._watched_instruments()
    assert ("crypto", "XBTUSD") in watched
    assert ("equity", "shel.uk") in watched
    assert ("equity", "bp.uk") not in watched
    assert len(watched) == 2  # XBTUSD deduped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -k "watch" -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute 'WATCHLIST_FILE'`

- [ ] **Step 3: Implement storage & resolution**

In `trading.py`, after `CRYPTO_TICKER_MAP_FILE = ...` (~line 36) add:

```python
WATCHLIST_FILE = PAPER_DIR / "watchlist.json"
VOLUME_HISTORY_FILE = PAPER_DIR / "volume-history.json"
```

Then add these functions near `load_book` (~line 695):

```python
def load_watchlist() -> dict:
    """Cross-asset watch list: {"items": [{raw, asset_class, instrument, added?}]}."""
    return _load_json_or(WATCHLIST_FILE, {"items": []})


def save_watchlist(wl: dict) -> None:
    _write_json_atomic(WATCHLIST_FILE, wl)


def resolve_watch_entry(token: str, asset_class: str | None = None) -> dict | None:
    """Resolve a /watch token into a watchlist entry, or None if unresolvable.

    Inference order is crypto → equity. Prediction is never inferred (a market
    can't be guessed from a ticker-shaped token): it needs an explicit class and a
    market id, validated via polygram_market.
    """
    token = token.strip()
    if asset_class == "prediction":
        if polygram_market(token) is None:
            return None
        return {"raw": token, "asset_class": "prediction", "instrument": token}
    if asset_class in (None, "crypto"):
        pair = resolve_kraken_pair(token, load_crypto_ticker_overrides())
        if pair:
            return {"raw": token, "asset_class": "crypto", "instrument": pair}
        if asset_class == "crypto":
            return None
    sym = resolve_stooq_symbol(token, load_instruments_cache(), load_ticker_overrides())
    if sym:
        return {"raw": token, "asset_class": "equity", "instrument": sym}
    return None


def _watched_instruments() -> list[tuple[str, str]]:
    """Union of watchlist entries and OPEN-position instruments, deduped by
    (asset_class, instrument)."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for it in load_watchlist().get("items", []):
        key = (it["asset_class"], it["instrument"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    for p in load_book().get("positions", []):
        if p.get("status") != "open":
            continue
        key = (p.get("asset_class", "equity"), p["instrument"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -k "watch" -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_monitor.py
git commit -m "feat: cross-asset watchlist storage + resolution"
```

---

### Task 5: `run_volume_monitor` orchestration (`trading.py`)

Sweeps the watched set, fetches volume per instrument (each wrapped so one failure can't abort the run), updates the baseline, and emits alert strings honoring the cooldown.

**Files:**
- Modify: `trading.py` (add after `_watched_instruments`)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
def _setup_monitor(monkeypatch, tmp_path, watched, volumes, history=None):
    monkeypatch.setattr(trading, "VOLUME_HISTORY_FILE", tmp_path / "vh.json")
    monkeypatch.setattr(trading, "_watched_instruments", lambda: watched)
    monkeypatch.setattr(trading, "fetch_volume", lambda ac, inst: volumes.get((ac, inst)))
    if history is not None:
        trading._write_json_atomic(tmp_path / "vh.json", history)


def test_monitor_emits_alert_on_spike(monkeypatch, tmp_path):
    prior = {"crypto:XBTUSD": {"samples": [100, 100, 100, 100, 100], "last_alert_ts": None}}
    _setup_monitor(monkeypatch, tmp_path, [("crypto", "XBTUSD")], {("crypto", "XBTUSD"): 500.0}, prior)
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    alerts = trading.run_volume_monitor(now=now)
    assert len(alerts) == 1
    assert "XBTUSD" in alerts[0]
    # last_alert_ts persisted and current sample appended
    hist = trading._load_json_or(tmp_path / "vh.json", {})
    assert hist["crypto:XBTUSD"]["last_alert_ts"] == now.isoformat()
    assert hist["crypto:XBTUSD"]["samples"][-1] == 500.0


def test_monitor_respects_cooldown(monkeypatch, tmp_path):
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    prior = {"crypto:XBTUSD": {"samples": [100, 100, 100, 100, 100], "last_alert_ts": recent}}
    _setup_monitor(monkeypatch, tmp_path, [("crypto", "XBTUSD")], {("crypto", "XBTUSD"): 500.0}, prior)
    alerts = trading.run_volume_monitor(now=now)
    assert alerts == []  # suppressed by cooldown
    # sample still appended (baseline keeps tracking during cooldown)
    hist = trading._load_json_or(tmp_path / "vh.json", {})
    assert hist["crypto:XBTUSD"]["samples"][-1] == 500.0


def test_monitor_skips_unpriceable_without_aborting(monkeypatch, tmp_path):
    prior = {
        "equity:a.us": {"samples": [100, 100, 100, 100, 100], "last_alert_ts": None},
        "equity:b.us": {"samples": [100, 100, 100, 100, 100], "last_alert_ts": None},
    }
    _setup_monitor(
        monkeypatch, tmp_path,
        [("equity", "a.us"), ("equity", "b.us")],
        {("equity", "a.us"): None, ("equity", "b.us"): 500.0},
        prior,
    )
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    alerts = trading.run_volume_monitor(now=now)
    assert len(alerts) == 1 and "b.us" in alerts[0]  # a.us skipped, b.us still alerts


def test_monitor_raising_fetch_does_not_abort(monkeypatch, tmp_path):
    monkeypatch.setattr(trading, "VOLUME_HISTORY_FILE", tmp_path / "vh.json")
    monkeypatch.setattr(trading, "_watched_instruments", lambda: [("equity", "boom.us"), ("crypto", "XBTUSD")])

    def _fetch(ac, inst):
        if inst == "boom.us":
            raise RuntimeError("network on fire")
        return 500.0

    monkeypatch.setattr(trading, "fetch_volume", _fetch)
    trading._write_json_atomic(
        tmp_path / "vh.json",
        {"crypto:XBTUSD": {"samples": [100, 100, 100, 100, 100], "last_alert_ts": None}},
    )
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    alerts = trading.run_volume_monitor(now=now)  # must not raise
    assert len(alerts) == 1 and "XBTUSD" in alerts[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -k "monitor" -v`
Expected: FAIL with `AttributeError: module 'trading' has no attribute 'run_volume_monitor'`

- [ ] **Step 3: Implement the orchestrator**

In `trading.py`, after `_watched_instruments`, add:

```python
def run_volume_monitor(now: datetime | None = None) -> list[str]:
    """Sweep watched instruments + open positions for volume anomalies.

    Returns a list of Telegram-HTML alert strings (empty when quiet). Updates
    volume-history.json (trailing samples + last_alert_ts) in place. Each
    instrument is isolated: a fetch returning None or raising is logged and
    skipped without aborting the sweep. `now` is injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    hist = _load_json_or(VOLUME_HISTORY_FILE, {})
    alerts: list[str] = []
    for asset_class, instrument in _watched_instruments():
        key = f"{asset_class}:{instrument}"
        entry = hist.get(key) or {"samples": [], "last_alert_ts": None}
        try:
            current = fetch_volume(asset_class, instrument)
        except Exception as e:
            log.warning(f"Volume fetch raised for {key}: {e}")
            current = None
        if current is None:
            continue
        prior = entry.get("samples", [])
        is_spike, ratio = _volume_anomaly(prior, current, _floor_for(asset_class))
        if is_spike and not _in_cooldown(entry.get("last_alert_ts"), now):
            baseline = sum(prior) / len(prior)
            alerts.append(_format_alert(asset_class, instrument, current, baseline, ratio))
            entry["last_alert_ts"] = now.isoformat()
        entry["samples"] = _append_sample(prior, current)
        hist[key] = entry
    _write_json_atomic(VOLUME_HISTORY_FILE, hist)
    return alerts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -k "monitor" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_monitor.py
git commit -m "feat: run_volume_monitor cross-asset sweep"
```

---

### Task 6: `mode_monitor` + dispatch wiring (`brief.py`)

**Files:**
- Modify: `brief.py` (`from trading import (...)` ~line 53; new `mode_monitor` near `mode_commands` ~line 1428; dispatch ~line 1444; usage string ~line 1463)
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_monitor.py`:

```python
import brief


def test_mode_monitor_sends_message_when_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr("trading.run_volume_monitor", lambda: ["📈 <b>crypto</b> XBTUSD: 5.0× avg volume (5.0k vs 1.0k)"])
    brief.mode_monitor()
    assert len(sent) == 1
    assert "Volume alerts" in sent[0]
    assert "XBTUSD" in sent[0]


def test_mode_monitor_silent_when_quiet(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr("trading.run_volume_monitor", lambda: [])
    brief.mode_monitor()
    assert sent == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitor.py -k "mode_monitor" -v`
Expected: FAIL with `AttributeError: module 'brief' has no attribute 'mode_monitor'`

- [ ] **Step 3: Implement `mode_monitor` and wire dispatch**

In `brief.py`, extend the `from trading import (` block (after `mark_to_market,`) with:

```python
    run_volume_monitor,
    load_watchlist,
    save_watchlist,
    resolve_watch_entry,
    price_position,
    _signal_return,
```

Add `mode_monitor` immediately after `mode_commands` (~line 1432):

```python
def mode_monitor():
    """Hourly cross-asset volume-anomaly alerts. Decoupled from the brief: its own
    cron mode, so a monitor failure can never delay or duplicate the morning brief."""
    log.info("=== MONITOR ===")
    alerts = run_volume_monitor()
    if alerts:
        telegram_send("🔔 <b>Volume alerts</b>\n\n" + "\n".join(alerts))
```

Add to the `dispatch` dict (after `"paper": mode_paper,`):

```python
        "monitor": mode_monitor,
```

Update the usage string:

```python
        print("Usage: brief.py [submit|collect|weekly|run|commands|monitor]")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitor.py -k "mode_monitor" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_monitor.py
git commit -m "feat: mode_monitor cron + dispatch wiring"
```

---

### Task 7: `/watch` and `/unwatch` commands (`brief.py`)

**Files:**
- Modify: `brief.py` (`_handle_telegram_update` ~line 360, before the final `else`; `HELP_TEXT` ~line 217)
- Test: `tests/test_commands.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_commands.py`:

```python
"""Phase 5 Telegram command handlers: /watch /unwatch /positions /performance."""

import brief
import trading


def _fb():
    return {"focus": [], "mute": [], "notes": []}


def _update(text):
    return {"message": {"text": text, "chat": {"id": brief.TELEGRAM_CHAT_ID}}}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda text: sent.append(text) or True)
    return sent


def test_watch_adds_inferred_crypto(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    items = trading.load_watchlist()["items"]
    assert items == [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]
    assert "crypto" in sent[0] and "XBTUSD" in sent[0]


def test_watch_explicit_prediction(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"question": "x"})
    brief._handle_telegram_update(_update("/watch prediction 0xabc"), _fb())
    items = trading.load_watchlist()["items"]
    assert items[0]["asset_class"] == "prediction" and items[0]["instrument"] == "0xabc"


def test_watch_unresolvable_reports_and_skips(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: None)
    brief._handle_telegram_update(_update("/watch NOTATHING"), _fb())
    assert trading.load_watchlist()["items"] == []
    assert "Couldn't resolve" in sent[0]


def test_watch_duplicate_is_noop(monkeypatch, tmp_path):
    _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    assert len(trading.load_watchlist()["items"]) == 1


def test_unwatch_removes(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    trading.save_watchlist({"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]})
    brief._handle_telegram_update(_update("/unwatch BTC"), _fb())
    assert trading.load_watchlist()["items"] == []
    assert "Unwatched" in sent[0]


def test_unwatch_missing_reports(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/unwatch GHOST"), _fb())
    assert "not on the watchlist" in sent[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -k "watch" -v`
Expected: FAIL (the `/watch` branch doesn't exist, so the handler hits the `else` → "Unknown command")

- [ ] **Step 3: Implement the branches**

In `brief.py` `_handle_telegram_update`, add these branches just before the final `else:` (the `Unknown command` branch):

```python
    elif text.startswith("/watch "):
        body = text[7:].strip()
        parts = body.split(maxsplit=1)
        if parts and parts[0] in ("equity", "crypto", "prediction") and len(parts) == 2:
            ac, token = parts[0], parts[1].strip()
        else:
            ac, token = None, body
        entry = resolve_watch_entry(token, asset_class=ac)
        if entry is None:
            telegram_send(
                f"⚠️ Couldn't resolve <b>{html.escape(token)}</b>"
                + (" (prediction needs an explicit market id: <code>/watch prediction &lt;id&gt;</code>)" if ac is None else "")
            )
        else:
            wl = load_watchlist()
            dup = any(
                i["asset_class"] == entry["asset_class"]
                and i["instrument"] == entry["instrument"]
                for i in wl["items"]
            )
            if not dup:
                entry["added"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                wl["items"].append(entry)
                save_watchlist(wl)
            telegram_send(
                f"👁️ Watching <b>{html.escape(entry['raw'])}</b> "
                f"({entry['asset_class']} → <code>{html.escape(entry['instrument'])}</code>)"
                + ("" if not dup else " — already watched")
            )

    elif text.startswith("/unwatch "):
        token = text[9:].strip()
        wl = load_watchlist()
        before = len(wl["items"])
        wl["items"] = [i for i in wl["items"] if i["raw"].lower() != token.lower()]
        if len(wl["items"]) < before:
            save_watchlist(wl)
            telegram_send(f"🚫 Unwatched <b>{html.escape(token)}</b>.")
        else:
            telegram_send(f"<b>{html.escape(token)}</b> is not on the watchlist.")
```

In `HELP_TEXT`, add before the `/reset` line:

```python
/watch [SYMBOL]
  Track an instrument for volume alerts (crypto/equity inferred).
  e.g. <code>/watch BTC</code> · <code>/watch prediction 0xMARKETID</code>

/unwatch [SYMBOL]
  Stop watching an instrument.

/positions — open positions with live marks
/performance — performance report + go-live gate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -k "watch" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -m "feat: /watch and /unwatch Telegram commands"
```

---

### Task 8: `/positions` and `/performance` commands (`brief.py`)

`/positions` lists open positions grouped by asset class with live marks; `/performance` reuses `validation.performance_report` (already imported into `brief.py`).

**Files:**
- Modify: `brief.py` (`_handle_telegram_update`, before the final `else`)
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands.py`:

```python
def test_positions_lists_open_with_marks(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        brief, "load_book",
        lambda: {"positions": [
            {"status": "open", "asset_class": "crypto", "instrument": "XBTUSD",
             "ticker": "BTC", "direction": "bullish", "entry_price": 100.0},
            {"status": "closed", "asset_class": "equity", "instrument": "bp.uk",
             "ticker": "BP", "direction": "bullish", "entry_price": 5.0},
        ]},
    )
    monkeypatch.setattr(brief, "price_position", lambda p: 150.0)
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "BTC" in sent[0] and "crypto" in sent[0]
    assert "+50.0%" in sent[0]
    assert "BP" not in sent[0]  # closed excluded


def test_positions_empty(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "No open positions" in sent[0]


def test_positions_unpriceable_shows_dash(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        brief, "load_book",
        lambda: {"positions": [
            {"status": "open", "asset_class": "equity", "instrument": "x.us",
             "ticker": "X", "direction": "bullish", "entry_price": 10.0},
        ]},
    )
    monkeypatch.setattr(brief, "price_position", lambda p: None)
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "—" in sent[0]


def test_performance_wraps_report(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(brief, "performance_report", lambda book: "📊 PERFORMANCE REPORT\nstub")
    brief._handle_telegram_update(_update("/performance"), _fb())
    assert "PERFORMANCE REPORT" in sent[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -k "positions or performance" -v`
Expected: FAIL (handler returns "Unknown command")

- [ ] **Step 3: Implement the branches**

In `brief.py` `_handle_telegram_update`, add before the final `else:`:

```python
    elif text == "/positions":
        book = load_book()
        opens = [p for p in book["positions"] if p.get("status") == "open"]
        if not opens:
            telegram_send("No open positions.")
        else:
            by_class: dict[str, list[str]] = {}
            for p in opens:
                mark = price_position(p)
                if mark is None:
                    line = f"  – {html.escape(p.get('ticker', p['instrument']))}: mark —"
                else:
                    ret = _signal_return(p["direction"], p["entry_price"], mark)
                    line = (
                        f"  – {html.escape(p.get('ticker', p['instrument']))}: "
                        f"{100 * ret:+.1f}%"
                    )
                by_class.setdefault(p.get("asset_class", "equity"), []).append(line)
            lines = ["<b>📂 Open positions</b>"]
            for ac in ("equity", "crypto", "prediction"):
                if by_class.get(ac):
                    lines.append(f"<b>{ac}</b>")
                    lines.extend(by_class[ac])
            telegram_send("\n".join(lines))

    elif text == "/performance":
        for chunk in split_html_message(performance_report(load_book())):
            telegram_send(chunk)
            time.sleep(0.4)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -k "positions or performance" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite + lint gate**

Run:
```
python -m pytest -q
ruff check .
ruff format --check .
```
Expected: all pass. If `ruff format --check` reports files, run `ruff format .` and stage them.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_commands.py
git commit -m "feat: /positions and /performance Telegram commands"
```

---

### Task 9: Wiring — compose service, env example, README

**Files:**
- Modify: `docker-compose.yml` (services block ~line 58; cron comment ~line 17)
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add the compose service**

In `docker-compose.yml`, after the `newsbrief-commands` service block, add:

```yaml
  newsbrief-monitor:
    <<: *newsbrief
    command: [monitor]
```

And add to the cron-schedule comment block (after the `*/30 ... newsbrief-commands` line):

```
#   0  *  * * *  docker compose run --rm newsbrief-monitor
```

- [ ] **Step 2: Document env knobs**

In `.env.example`, add a section:

```
# ── Volume monitor (Phase 5) ──────────────────────────────────────────────────
# VOL_SPIKE_MULT=2.5            # alert when current/trailing-mean >= this
# VOL_TRAILING_N=20             # trailing samples kept per instrument
# VOL_MIN_SAMPLES=5             # warm-up: min prior samples before alerting
# VOL_ALERT_COOLDOWN_HRS=12     # per-instrument re-alert suppression window
# VOL_FLOOR_EQUITY=0            # absolute volume floor (0 = off)
# VOL_FLOOR_CRYPTO=0
# VOL_FLOOR_PREDICTION=0
```

- [ ] **Step 3: Document the mode + commands in README**

In `README.md`, find the modes/cron section (the `MODE` table near line 13 and any command list) and add:
- a `monitor` row: "hourly cross-asset volume-anomaly alerts" with the cron line `0 * * * * docker compose run --rm newsbrief-monitor`;
- the four new commands in the Telegram command list: `/watch <symbol>`, `/unwatch <symbol>`, `/positions`, `/performance`.

(Match the existing table/formatting style — read the surrounding lines first.)

- [ ] **Step 4: Verify nothing broke**

Run: `python -m pytest -q`
Expected: all pass (docs/compose changes don't affect tests, but confirms the tree is green).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example README.md
git commit -m "docs: wire monitor cron service + document VOL_* knobs and new commands"
```

---

## Self-Review

**Spec coverage:**
- `monitor` cron (cross-asset volume anomalies) → Tasks 2–6, 9. ✅
- Ratio + floor + warm-up anomaly rule → Task 3. ✅
- Per-instrument cooldown dedup → Tasks 3, 5. ✅
- Volume sources Stooq/Kraken/PolyGram (graceful prediction degradation) → Task 2. ✅
- `watchlist.json` cross-asset (supersedes `polygram_watchlist.json`) → Task 4. ✅
- `volume-history.json` baseline state → Tasks 3, 5. ✅
- `/watch` (inferred class + explicit override, prediction needs id) → Task 7. ✅
- `/unwatch` → Task 7. ✅
- `/positions` (open-only, grouped, unpriceable handled) → Task 8. ✅
- `/performance` (reuses `performance_report`) → Task 8. ✅
- Env knobs documented → Tasks 1, 9. ✅
- Compose service + hourly cron + README → Task 9. ✅
- Failure isolation (monitor independent of brief; per-instrument try/except) → Tasks 5, 6. ✅
- Tests mirror existing patterns; all listed test buckets covered → every task. ✅

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows complete code. ✅

**Type consistency:** `resolve_watch_entry(token, asset_class=None) -> dict|None` with keys `{raw, asset_class, instrument, (added)}` used consistently in Tasks 4, 7. `run_volume_monitor(now=None) -> list[str]` consistent in Tasks 5, 6. `fetch_volume(asset_class, instrument)` consistent in Tasks 2, 5. `_volume_anomaly(prior, current, floor) -> (bool, float)` consistent in Tasks 3, 5. `price_position` / `_signal_return` signatures match their `trading.py` definitions. ✅

**Note on imports:** `_signal_return` and `price_position` are imported into `brief.py` from `trading` in Task 6; both are used in Task 8 — Task 6 must land before Task 8 (it does, by order).
