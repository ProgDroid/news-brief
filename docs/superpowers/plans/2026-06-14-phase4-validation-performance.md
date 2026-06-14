# Phase 4 — Validation / Performance Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the primitive weekly `paper_scorecard` into a real validation/performance layer — per-trade cost haircut + market-index benchmark stamped onto closed positions, dimensional aggregation, a per-asset-class go-live readiness gate, a performance-feedback prompt block, and the unified daily trade message.

**Architecture:** `book.json` stays the single source of truth (no `performance.json`). Lifecycle **stamping** (benchmark entry + prediction orderbook spread at open; haircut/net_return/benchmark_return/edge at close) lives in `trading.py`. All **pure analysis** (aggregation, gate, report, prompt block, daily trade message) lives in a new `validation.py` module operating over a `book` dict. One-way imports preserved: `common ← trading`, `common ← validation`, `{trading, validation} ← brief`.

**Tech Stack:** Python 3, `requests`, stdlib `statistics`/`json`, pytest. Pricing via existing `fetch_stooq_price` (equity `^spx`), `fetch_kraken_price` (crypto BTC), `_polygram_get` (prediction orderbook).

**Governing spec:** `docs/superpowers/specs/2026-06-14-phase4-validation-performance-design.md`.

**Testing convention (carry from Phase 1–3):** when a test *monkeypatches* behaviour, patch it on the module whose function is *under test* (e.g. `trading.fetch_stooq_price`, `brief.telegram_send`) — module-level names are bound at import. When a test just *calls a pure function*, either namespace works. Pre-push gate (per [[brief-local-run]]): `ruff check . && ruff format --check . && pytest -q` — stage every reformatted file or CI fails. Run Python via the PowerShell tool ([[python-via-powershell]]); commit via the Bash tool (PowerShell prepends a BOM to commit subjects).

---

## File structure

| File | Change | Responsibility added |
|------|--------|----------------------|
| `common.py` | Modify | Phase-4 config constants (haircut bps, gate knobs). |
| `trading.py` | Modify | `fetch_benchmark_level`, `_fetch_pg_half_spread`, `_stamp_open_benchmark` (open) + wiring; `_haircut_fraction`, `_stamp_close_metrics` (close) + wiring into 3 close paths. Remove `paper_scorecard` (moves to validation). |
| `validation.py` | Create | `aggregate_performance`, `evaluate_gate`, `record_gate_history`, `performance_report` (supersedes `paper_scorecard`), `performance_prompt_block`, `daily_trade_message`. |
| `brief.py` | Modify | Repoint imports; `mode_weekly` (record gate history + enhanced report); `build_daily_prompt` (+ perf-block param) + `mode_submit` wiring; `mode_collect` unified daily trade message. |
| `tests/test_validation.py` | Create | Aggregation, gate, report, prompt block, daily message. |
| `tests/test_trading.py` | Modify | Stamping hooks; drop `paper_scorecard` export assertion. |

---

## Task 1: Phase-4 config constants (`common.py`)

**Files:**
- Modify: `common.py` (constants area, near `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` ~line 70)
- Test: `tests/test_validation.py` (created in Task 4; this task is config only)

- [ ] **Step 1: Add the constants** after the PolyGram env lines in `common.py`:

```python
# ── Phase 4: validation / performance ─────────────────────────────────────────
# Round-trip cost haircut (basis points) applied to gross return at close, by asset
# class. Prediction uses the real orderbook half-spread when available (see trading
# ._fetch_pg_half_spread); this is the fallback / momentum-exit cost.
HAIRCUT_BPS_EQUITY = int(os.environ.get("HAIRCUT_BPS_EQUITY", "10"))
HAIRCUT_BPS_CRYPTO = int(os.environ.get("HAIRCUT_BPS_CRYPTO", "26"))
HAIRCUT_BPS_PREDICTION = int(os.environ.get("HAIRCUT_BPS_PREDICTION", "200"))
# Go-live readiness gate (per asset class). Informational — nothing auto-enables live.
GATE_MIN_TRADES = int(os.environ.get("GATE_MIN_TRADES", "30"))
GATE_MIN_HIT_RATE = float(os.environ.get("GATE_MIN_HIT_RATE", "0.55"))
GATE_SUSTAINED_EVALS = int(os.environ.get("GATE_SUSTAINED_EVALS", "2"))
```

- [ ] **Step 2: Verify import** — Run (PowerShell): `python -c "import common; print(common.HAIRCUT_BPS_CRYPTO, common.GATE_MIN_TRADES, common.GATE_MIN_HIT_RATE)"`
Expected: `26 30 0.55`

- [ ] **Step 3: Commit**

```bash
git add common.py
git commit -m "feat: phase-4 haircut + go-live gate config constants"
```

---

## Task 2: Open-time stamping — benchmark entry + prediction spread (`trading.py`)

**Files:**
- Modify: `trading.py` — import constants; add `fetch_benchmark_level`, `_fetch_pg_half_spread`, `_stamp_open_benchmark`; call the stamper in `mode_paper` and `_open_prediction_positions`.
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_trading.py`:

```python
def test_stamp_open_benchmark_equity(monkeypatch):
    import trading
    monkeypatch.setattr(trading, "fetch_stooq_price", lambda s: 5000.0)
    p = {"asset_class": "equity"}
    trading._stamp_open_benchmark(p)
    assert p["benchmark_entry"] == 5000.0
    assert p["entry_spread"] is None


def test_stamp_open_benchmark_prediction(monkeypatch):
    import trading
    monkeypatch.setattr(trading, "_fetch_pg_half_spread", lambda t: 0.02)
    p = {"asset_class": "prediction", "token_id": "tok123"}
    trading._stamp_open_benchmark(p)
    assert p["benchmark_entry"] is None
    assert p["entry_spread"] == 0.02


def test_stamp_open_benchmark_best_effort(monkeypatch):
    import trading

    def _boom(_):
        raise RuntimeError("network down")

    monkeypatch.setattr(trading, "fetch_stooq_price", _boom)
    p = {"asset_class": "equity"}
    trading._stamp_open_benchmark(p)  # must not raise
    assert p["benchmark_entry"] is None


def test_fetch_pg_half_spread_parses_levels(monkeypatch):
    import trading
    monkeypatch.setattr(
        trading,
        "_polygram_get",
        lambda path: {"bids": [{"price": "0.40"}], "asks": [{"price": "0.50"}]},
    )
    # mid 0.45, half-spread 0.05 → 0.05/0.45
    assert abs(trading._fetch_pg_half_spread("tok") - (0.05 / 0.45)) < 1e-9


def test_fetch_pg_half_spread_none_on_garbage(monkeypatch):
    import trading
    monkeypatch.setattr(trading, "_polygram_get", lambda path: None)
    assert trading._fetch_pg_half_spread("tok") is None
```

- [ ] **Step 2: Run to verify they fail** — Run: `pytest tests/test_trading.py -k "stamp_open or half_spread" -q`
Expected: FAIL (`AttributeError: module 'trading' has no attribute '_stamp_open_benchmark'`).

- [ ] **Step 3: Extend the `common` import** in `trading.py` (the `from common import (...)` block, ~lines 11-25) — add these names:

```python
    HAIRCUT_BPS_EQUITY,
    HAIRCUT_BPS_CRYPTO,
    HAIRCUT_BPS_PREDICTION,
```

- [ ] **Step 4: Add the helpers** — insert immediately **after** `fetch_kraken_price` (so `resolve_kraken_pair`/`load_crypto_ticker_overrides`/`fetch_kraken_price` are already defined by call time; if ordering trips the linter, place after `price_position`). Use this code:

```python
def fetch_benchmark_level(asset_class: str) -> float | None:
    """Current benchmark index level for an asset class (best-effort, None on failure).

    Equity → S&P 500 (^spx via Stooq); crypto → BTC/XBT (Kraken); prediction → None
    (naive coin-flip baseline, handled at close as benchmark_return=0).
    """
    if asset_class == "equity":
        return fetch_stooq_price("^spx")
    if asset_class == "crypto":
        pair = resolve_kraken_pair("BTC", load_crypto_ticker_overrides())
        return fetch_kraken_price(pair) if pair else None
    return None


def _fetch_pg_half_spread(token_id: str) -> float | None:
    """Best bid/ask half-spread as a fraction of mid, from PolyGram's orderbook.

    Returns None when uncredentialed/unavailable/malformed. Tolerant of both
    [{"price": "0.4"}] and [["0.4", size]] level shapes.
    """
    data = _polygram_get(f"/orderbook/{token_id}")
    if not isinstance(data, dict):
        return None

    def _best(levels):
        if not levels:
            return None
        lvl = levels[0]
        try:
            raw = lvl["price"] if isinstance(lvl, dict) else lvl[0]
            return float(raw)
        except (TypeError, ValueError, KeyError, IndexError):
            return None

    bid = _best(data.get("bids"))
    ask = _best(data.get("asks"))
    if bid is None or ask is None or ask < bid:
        return None
    mid = (ask + bid) / 2
    if mid <= 0:
        return None
    return (ask - bid) / 2 / mid


def _stamp_open_benchmark(p: dict) -> None:
    """Stamp benchmark_entry (+ prediction entry_spread) on a freshly opened position.

    Best-effort: any fetch failure leaves the field None and never raises.
    """
    ac = p.get("asset_class", "equity")
    if ac == "prediction":
        p["benchmark_entry"] = None
        tok = p.get("token_id")
        try:
            p["entry_spread"] = _fetch_pg_half_spread(tok) if tok else None
        except Exception as e:
            log.warning(f"PG spread fetch failed for {p.get('ticker')}: {e}")
            p["entry_spread"] = None
        return
    p["entry_spread"] = None
    try:
        p["benchmark_entry"] = fetch_benchmark_level(ac)
    except Exception as e:
        log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
        p["benchmark_entry"] = None
```

- [ ] **Step 5: Run to verify they pass** — Run: `pytest tests/test_trading.py -k "stamp_open or half_spread" -q`
Expected: PASS.

- [ ] **Step 6: Wire the stamper into the open paths.** In `mode_paper`, immediately **after** `book["positions"].append({...})` for the equity/crypto position (before `open_keys.add(...)`), add:

```python
            _stamp_open_benchmark(book["positions"][-1])
```

In `_open_prediction_positions`, immediately **after** `book["positions"].append({...})` (before `open_keys.add(key)`), add:

```python
        _stamp_open_benchmark(book["positions"][-1])
```

- [ ] **Step 7: Run the full trading suite** — Run: `pytest tests/test_trading.py tests/test_crypto.py tests/test_prediction.py -q`
Expected: PASS (existing open-path tests still green; new fields are additive). If a prediction open test asserts on the network, it is creds-gated and `_fetch_pg_half_spread` returns None under no creds.

- [ ] **Step 8: Commit**

```bash
git add trading.py tests/test_trading.py
git commit -m "feat: stamp benchmark entry + prediction orderbook spread at open"
```

---

## Task 3: Close-time finalizer — haircut / net_return / benchmark_return / edge (`trading.py`)

**Files:**
- Modify: `trading.py` — add `_haircut_fraction`, `_stamp_close_metrics`; call from `_close_position_at_market`, `mark_to_market` (horizon close), `_settle_prediction`.
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_trading.py`:

```python
def _closed_equity(**kw):
    p = {
        "ticker": "SHEL", "asset_class": "equity", "direction": "bullish",
        "realized_return": 0.10, "benchmark_entry": 100.0, "play_type": None,
    }
    p.update(kw)
    return p


def test_stamp_close_metrics_equity_with_benchmark(monkeypatch):
    import trading
    monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: 104.0)
    p = _closed_equity()
    trading._stamp_close_metrics(p, "2026-06-14")
    assert p["haircut"] == trading.HAIRCUT_BPS_EQUITY / 10_000
    assert abs(p["net_return"] - (0.10 - 0.0010)) < 1e-9
    assert abs(p["benchmark_return"] - 0.04) < 1e-9  # (104-100)/100
    assert abs(p["edge"] - (p["net_return"] - 0.04)) < 1e-9


def test_stamp_close_metrics_legacy_no_benchmark(monkeypatch):
    import trading
    monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: 104.0)
    p = _closed_equity(benchmark_entry=None)
    trading._stamp_close_metrics(p, "2026-06-14")
    assert p["net_return"] is not None
    assert p["benchmark_return"] is None
    assert p["edge"] is None


def test_stamp_close_metrics_benchmark_fetch_failure(monkeypatch):
    import trading

    def _boom(_):
        raise RuntimeError("down")

    monkeypatch.setattr(trading, "fetch_benchmark_level", _boom)
    p = _closed_equity()
    trading._stamp_close_metrics(p, "2026-06-14")  # must not raise
    assert p["net_return"] is not None
    assert p["benchmark_return"] is None
    assert p["edge"] is None


def test_stamp_close_metrics_prediction_resolution():
    import trading
    p = {
        "ticker": "mkt1", "asset_class": "prediction", "play_type": "resolution",
        "realized_return": 0.30, "entry_spread": 0.02, "benchmark_entry": None,
    }
    trading._stamp_close_metrics(p, "2026-06-14")
    # resolution settles at 1/0 — entry spread only, no exit leg
    assert p["haircut"] == 0.02
    assert abs(p["net_return"] - 0.28) < 1e-9
    assert p["benchmark_return"] == 0.0  # naive coin-flip baseline
    assert abs(p["edge"] - 0.28) < 1e-9


def test_stamp_close_metrics_prediction_momentum_fallback():
    import trading
    p = {
        "ticker": "mkt2", "asset_class": "prediction", "play_type": "momentum",
        "realized_return": 0.10, "entry_spread": None, "benchmark_entry": None,
    }
    trading._stamp_close_metrics(p, "2026-06-14")
    bps = trading.HAIRCUT_BPS_PREDICTION / 10_000
    assert abs(p["haircut"] - 2 * bps) < 1e-9  # entry fallback + exit bps
    assert abs(p["net_return"] - (0.10 - 2 * bps)) < 1e-9
```

- [ ] **Step 2: Run to verify they fail** — Run: `pytest tests/test_trading.py -k "stamp_close" -q`
Expected: FAIL (`AttributeError: ... '_stamp_close_metrics'`).

- [ ] **Step 3: Add the finalizer** — insert in `trading.py` immediately **after** `_stamp_open_benchmark` (from Task 2):

```python
def _haircut_fraction(p: dict) -> float:
    """Round-trip cost fraction for a closed position, by asset class.

    Prediction: entry = real orderbook half-spread if captured at open, else the
    config fallback; resolution settles at 1/0 (entry leg only), momentum adds a
    config-bps exit leg. Equity/crypto: a single config round-trip constant.
    """
    ac = p.get("asset_class", "equity")
    if ac == "prediction":
        entry = p.get("entry_spread")
        if entry is None:
            entry = HAIRCUT_BPS_PREDICTION / 10_000
        if p.get("play_type") == "momentum":
            return entry + HAIRCUT_BPS_PREDICTION / 10_000
        return entry
    if ac == "crypto":
        return HAIRCUT_BPS_CRYPTO / 10_000
    return HAIRCUT_BPS_EQUITY / 10_000


def _stamp_close_metrics(p: dict, day: str) -> None:
    """Stamp haircut/net_return/benchmark_return/edge on a just-closed position.

    Call AFTER status is set to "closed" and realized_return is stamped. Best-effort
    on the benchmark fetch — never raises out of a close path. A fetch failure or a
    legacy position with no benchmark_entry leaves benchmark_return/edge None, but
    net_return is always set.
    """
    gross = p.get("realized_return")
    if gross is None:
        return
    haircut = _haircut_fraction(p)
    p["haircut"] = haircut
    net = gross - haircut
    p["net_return"] = net
    ac = p.get("asset_class", "equity")
    bench = None
    if ac == "prediction":
        bench = 0.0  # naive coin-flip baseline
    else:
        entry = p.get("benchmark_entry")
        if entry:
            try:
                level = fetch_benchmark_level(ac)
            except Exception as e:
                log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
                level = None
            if level is not None:
                bench = _signal_return("bullish", entry, level)
    p["benchmark_return"] = bench
    p["edge"] = (net - bench) if bench is not None else None
```

- [ ] **Step 4: Run to verify they pass** — Run: `pytest tests/test_trading.py -k "stamp_close" -q`
Expected: PASS.

- [ ] **Step 5: Wire the finalizer into all three close paths.**

In `_close_position_at_market`, **before** `return True` (after `p["closed_date"] = day`):

```python
    _stamp_close_metrics(p, day)
```

In `mark_to_market`, inside the horizon-close branch, **after** `p["realized_return"] = p["checkpoints"][PAPER_CLOSE_HORIZON]["return"]`:

```python
            _stamp_close_metrics(p, today_str)
```

In `_settle_prediction`, **after** `p["realized_return"] = ret`:

```python
    _stamp_close_metrics(p, day)
```

- [ ] **Step 6: Run the full trading suite** — Run: `pytest tests/test_trading.py tests/test_crypto.py tests/test_prediction.py -q`
Expected: PASS. Existing close/MtM tests may now also see the new fields; they assert on `realized_return`/`status`, which are unchanged, so they stay green. If any close test runs without monkeypatching `fetch_benchmark_level` and its fixture has a truthy `benchmark_entry`, add `monkeypatch.setattr(trading, "fetch_benchmark_level", lambda ac: None)` to keep it hermetic (no network).

- [ ] **Step 7: Commit**

```bash
git add trading.py tests/test_trading.py
git commit -m "feat: stamp haircut/net_return/benchmark/edge on close (3 paths)"
```

---

## Task 4: `validation.py` — dimensional aggregation

**Files:**
- Create: `validation.py`
- Test: `tests/test_validation.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_validation.py`:

```python
import validation


def _closed(asset_class, net, edge=None, confidence=None, play_type=None,
            thesis_ref=None):
    return {
        "status": "closed", "asset_class": asset_class, "net_return": net,
        "edge": edge, "confidence": confidence, "play_type": play_type,
        "thesis_ref": thesis_ref,
    }


def test_aggregate_overall_hit_rate_and_means():
    book = {"positions": [
        _closed("equity", 0.10, edge=0.04),
        _closed("equity", -0.05, edge=-0.02),
        _closed("crypto", 0.20, edge=0.10),
    ]}
    agg = validation.aggregate_performance(book)
    o = agg["overall"]
    assert o["n"] == 3
    assert abs(o["hit_rate"] - (100.0 * 2 / 3)) < 1e-9
    assert abs(o["mean_net"] - (0.25 / 3)) < 1e-9
    assert o["median_net"] == 0.10
    assert abs(o["mean_edge"] - (0.12 / 3)) < 1e-9


def test_aggregate_by_dimension():
    book = {"positions": [
        _closed("equity", 0.10, confidence="high"),
        _closed("equity", -0.05, confidence="medium"),
        _closed("crypto", 0.20, confidence="high"),
    ]}
    agg = validation.aggregate_performance(book)
    assert agg["dimensions"]["asset_class"]["equity"]["n"] == 2
    assert agg["dimensions"]["asset_class"]["crypto"]["n"] == 1
    assert agg["dimensions"]["confidence"]["high"]["n"] == 2


def test_aggregate_excludes_no_net_return():
    # pre-Phase-4 closed positions have no net_return → excluded entirely
    book = {"positions": [
        {"status": "closed", "asset_class": "equity"},  # legacy, no net_return
        _closed("equity", 0.10),
    ]}
    assert validation.aggregate_performance(book)["overall"]["n"] == 1


def test_aggregate_empty_book():
    assert validation.aggregate_performance({"positions": []})["overall"] is None
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/test_validation.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'validation'`).

- [ ] **Step 3: Create `validation.py`** with the module header + aggregation:

```python
#!/usr/bin/env python3
"""Phase-4 validation / performance layer: pure analysis over the trading book.

Reads closed positions from a book dict (book.json is the single source of truth;
benchmark/haircut are stamped onto each closed position by trading.py at close).
No network, no mutation. Builds the weekly performance report, the go-live readiness
gate, the daily prompt-feedback block, and the unified daily trade message.
"""

import statistics

from common import (
    DATA_DIR,
    log,
    _write_json_atomic,
    _load_json_or,
    GATE_MIN_TRADES,
    GATE_MIN_HIT_RATE,
    GATE_SUSTAINED_EVALS,
)

GATE_HISTORY_FILE = DATA_DIR / "paper" / "gate_history.json"
_DIMENSIONS = ("asset_class", "confidence", "play_type", "thesis_ref")
_ASSET_CLASSES = ("equity", "crypto", "prediction")


def _stats(positions: list) -> dict | None:
    """Net-return stats for a group of closed positions; None if none are scored.

    Positions without net_return (pre-Phase-4 closes) are excluded. mean_edge is
    over the subset carrying a non-null edge (legacy/benchmark-failed → excluded).
    """
    nets = [p["net_return"] for p in positions if p.get("net_return") is not None]
    if not nets:
        return None
    edges = [p["edge"] for p in positions if p.get("edge") is not None]
    return {
        "n": len(nets),
        "hit_rate": 100.0 * sum(1 for r in nets if r > 0) / len(nets),
        "mean_net": sum(nets) / len(nets),
        "median_net": statistics.median(nets),
        "mean_edge": (sum(edges) / len(edges)) if edges else None,
        "n_edge": len(edges),
    }


def aggregate_performance(book: dict) -> dict:
    """Overall + per-dimension net stats over the book's closed positions."""
    closed = [p for p in book.get("positions", []) if p.get("status") == "closed"]
    dims = {}
    for dim in _DIMENSIONS:
        groups: dict = {}
        for p in closed:
            key = p.get(dim)
            if key is None:
                continue
            groups.setdefault(key, []).append(p)
        dims[dim] = {k: s for k, v in groups.items() if (s := _stats(v))}
    return {"overall": _stats(closed), "dimensions": dims}
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/test_validation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add validation.py tests/test_validation.py
git commit -m "feat: validation.py dimensional performance aggregation"
```

---

## Task 5: `validation.py` — go-live gate + gate history

**Files:**
- Modify: `validation.py` — add `record_gate_history`, `evaluate_gate`.
- Test: `tests/test_validation.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_validation.py`:

```python
def _many(asset_class, n, net, edge):
    return [_closed(asset_class, net, edge=edge) for _ in range(n)]


def test_gate_not_ready_too_few_trades(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    book = {"positions": _many("equity", 5, 0.10, 0.05)}
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is False
    assert "closed" in res["equity"]["reason"]


def test_gate_ready_when_all_criteria_met(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    # seed two positive prior evals for the sustained-window check
    validation._write_json_atomic(hist, {"equity": [0.03, 0.04]})
    book = {"positions": _many("equity", 30, 0.10, 0.05)}  # all wins, edge +
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is True


def test_gate_not_ready_when_not_sustained(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    validation._write_json_atomic(hist, {"equity": [-0.01, 0.04]})  # one negative
    book = {"positions": _many("equity", 30, 0.10, 0.05)}
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is False
    assert "sustained" in res["equity"]["reason"] or "evals" in res["equity"]["reason"]


def test_record_gate_history_appends(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    book = {"positions": _many("crypto", 3, 0.20, 0.10)}
    validation.record_gate_history(book)
    data = validation._load_json_or(hist, {})
    assert len(data["crypto"]) == 1
    assert abs(data["crypto"][0] - 0.10) < 1e-9
    assert data["equity"] == [None]  # no equity trades → null entry
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/test_validation.py -k gate -q`
Expected: FAIL (`AttributeError: ... 'evaluate_gate'`).

- [ ] **Step 3: Implement** — append to `validation.py`:

```python
def record_gate_history(book: dict) -> None:
    """Append this evaluation's per-asset mean edge to gate_history.json.

    Called once per weekly run BEFORE evaluate_gate, so the sustained-window check
    includes the current week. A null entry is recorded for asset classes with no
    scored trades this period.
    """
    per_asset = aggregate_performance(book)["dimensions"].get("asset_class", {})
    history = _load_json_or(GATE_HISTORY_FILE, {}) or {}
    for ac in _ASSET_CLASSES:
        s = per_asset.get(ac)
        history.setdefault(ac, []).append(s["mean_edge"] if s else None)
    _write_json_atomic(GATE_HISTORY_FILE, history)


def evaluate_gate(book: dict) -> dict:
    """Per-asset-class go-live readiness against the configured criteria.

    Each entry: {"ready": bool, "reason": str}. The gate is informational —
    nothing in the system auto-enables live trading.
    """
    per_asset = aggregate_performance(book)["dimensions"].get("asset_class", {})
    history = _load_json_or(GATE_HISTORY_FILE, {}) or {}
    out = {}
    for ac in _ASSET_CLASSES:
        s = per_asset.get(ac)
        if s is None:
            out[ac] = {"ready": False, "reason": "no closed trades yet"}
            continue
        recent = history.get(ac, [])[-GATE_SUSTAINED_EVALS:]
        sustained = len(recent) >= GATE_SUSTAINED_EVALS and all(
            e is not None and e > 0 for e in recent
        )
        if s["n"] < GATE_MIN_TRADES:
            reason = f"need {GATE_MIN_TRADES} closed trades, have {s['n']}"
        elif s["mean_edge"] is None or s["mean_edge"] <= 0:
            reason = "mean edge over benchmark not positive"
        elif s["hit_rate"] < GATE_MIN_HIT_RATE * 100:
            reason = f"net hit-rate {s['hit_rate']:.0f}% < {GATE_MIN_HIT_RATE * 100:.0f}%"
        elif not sustained:
            reason = f"edge not positive across last {GATE_SUSTAINED_EVALS} evals"
        else:
            out[ac] = {"ready": True, "reason": "all criteria met"}
            continue
        out[ac] = {"ready": False, "reason": reason}
    return out
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/test_validation.py -k gate -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add validation.py tests/test_validation.py
git commit -m "feat: go-live readiness gate + gate history (validation.py)"
```

---

## Task 6: `validation.py` — enhanced report (supersedes `paper_scorecard`); wire `mode_weekly`

**Files:**
- Modify: `validation.py` — add `performance_report`.
- Modify: `trading.py` — remove `paper_scorecard`.
- Modify: `brief.py` — repoint import; `mode_weekly` records gate history + sends `performance_report`.
- Test: `tests/test_validation.py`, `tests/test_trading.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_validation.py`:

```python
def test_performance_report_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    book = {"positions": [
        _closed("equity", 0.10, edge=0.04, confidence="high", thesis_ref="oil"),
        _closed("equity", -0.05, edge=-0.02, confidence="medium", thesis_ref="oil"),
        _closed("crypto", 0.20, edge=0.10, confidence="high"),
    ]}
    out = validation.performance_report(book)
    assert "PERFORMANCE" in out
    assert "equity" in out
    assert "Go-live" in out or "go-live" in out.lower()


def test_performance_report_empty_book(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    out = validation.performance_report({"positions": []})
    assert "No closed" in out or "no closed" in out.lower()
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/test_validation.py -k report -q`
Expected: FAIL (`AttributeError: ... 'performance_report'`).

- [ ] **Step 3: Implement `performance_report`** — append to `validation.py`:

```python
def _fmt(s: dict) -> str:
    edge = f"{100 * s['mean_edge']:+.1f}%" if s["mean_edge"] is not None else "n/a"
    return (
        f"{s['hit_rate']:.0f}% hit · net {100 * s['mean_net']:+.1f}% "
        f"· edge {edge} (n={s['n']})"
    )


def performance_report(book: dict) -> str:
    """Telegram-HTML weekly performance report: overall + dimensions + go-live gate.

    Supersedes the old paper_scorecard. Pure — gate history is read, not written
    (record_gate_history is called separately in the weekly job).
    """
    agg = aggregate_performance(book)
    overall = agg["overall"]
    lines = ["<b>📊 PERFORMANCE REPORT</b>"]
    if overall is None:
        lines.append("No closed trades yet — nothing to score.")
        return "\n".join(lines)

    lines.append(f"• Overall: {_fmt(overall)}")
    for dim in _DIMENSIONS:
        groups = agg["dimensions"].get(dim, {})
        if not groups:
            continue
        lines.append(f"<b>by {dim}</b>")
        for key, s in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"  – {key}: {_fmt(s)}")

    # Chronically-wrong theses (negative mean net over a meaningful sample).
    bad = [
        (k, s)
        for k, s in agg["dimensions"].get("thesis_ref", {}).items()
        if s["n"] >= 3 and s["mean_net"] < 0
    ]
    if bad:
        lines.append("<b>⚠ chronically wrong</b> (consider /mute or /thesis):")
        for k, s in bad:
            lines.append(f"  – {k}: net {100 * s['mean_net']:+.1f}% (n={s['n']})")

    lines.append("<b>🚦 Go-live gate</b>")
    gate = evaluate_gate(book)
    for ac in _ASSET_CLASSES:
        g = gate[ac]
        mark = "✅ READY" if g["ready"] else "⛔ not ready"
        lines.append(f"  – {ac}: {mark} — {g['reason']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/test_validation.py -k report -q`
Expected: PASS.

- [ ] **Step 5: Remove `paper_scorecard` from `trading.py`.** Delete the entire `paper_scorecard` function (the `def paper_scorecard(book: dict) -> str:` block). Then update `tests/test_trading.py` line ~15: in the export/attribute assertion list, **remove** the `"paper_scorecard",` entry so the test no longer requires it on `trading`.

- [ ] **Step 6: Repoint the import in `brief.py`.** In the `from trading import (...)` block (~lines 53-61), **remove** `paper_scorecard,`. Immediately after that block, add:

```python
from validation import (
    performance_report,
    record_gate_history,
)
```

- [ ] **Step 7: Wire `mode_weekly`.** Replace the line `telegram_send(paper_scorecard(book))` with:

```python
    record_gate_history(book)
    telegram_send(performance_report(book))
```

(The preceding `mark_to_market(book, today_str)` + `save_book(book)` calls are unchanged; `record_gate_history` reads the freshly-marked book before the report evaluates the gate.)

- [ ] **Step 8: Run the suites** — Run: `pytest tests/test_trading.py tests/test_validation.py -q` then `python -c "import brief"`.
Expected: PASS; `import brief` succeeds (no dangling `paper_scorecard` reference).

- [ ] **Step 9: Commit**

```bash
git add validation.py trading.py brief.py tests/test_validation.py tests/test_trading.py
git commit -m "feat: enhanced weekly performance report supersedes paper_scorecard"
```

---

## Task 7: Performance-feedback prompt block; inject into `build_daily_prompt`

**Files:**
- Modify: `validation.py` — add `performance_prompt_block`.
- Modify: `brief.py` — `build_daily_prompt` gains a `perf_block` param; `mode_submit` builds + passes it.
- Test: `tests/test_validation.py`, `tests/test_signals.py` (or wherever `build_daily_prompt` is tested).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_validation.py`:

```python
def test_prompt_block_requires_min_sample():
    # 4 trades < n>=5 floor → empty
    book = {"positions": [_closed("equity", 0.1, confidence="high") for _ in range(4)]}
    assert validation.performance_prompt_block(book) == ""


def test_prompt_block_includes_qualifying_dimension():
    book = {"positions": [
        _closed("equity", 0.1, edge=0.03, confidence="high") for _ in range(6)
    ]}
    out = validation.performance_prompt_block(book)
    assert out  # non-empty
    assert "high" in out or "equity" in out
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/test_validation.py -k prompt_block -q`
Expected: FAIL (`AttributeError: ... 'performance_prompt_block'`).

- [ ] **Step 3: Implement** — append to `validation.py`:

```python
_PROMPT_MIN_N = 5  # don't feed the model dimensions with tiny, noisy samples


def performance_prompt_block(book: dict) -> str:
    """Compact track-record block for the daily prompt (recalibration, not rules).

    Surfaces realized net hit-rate + edge by asset_class, confidence, and thesis_ref
    for dimensions with at least _PROMPT_MIN_N closed trades. Returns "" when nothing
    qualifies (so the caller adds nothing to the prompt).
    """
    agg = aggregate_performance(book)
    rows = []
    for dim in ("asset_class", "confidence", "thesis_ref"):
        for key, s in agg["dimensions"].get(dim, {}).items():
            if s["n"] < _PROMPT_MIN_N:
                continue
            edge = (
                f", edge {100 * s['mean_edge']:+.0f}%"
                if s["mean_edge"] is not None
                else ""
            )
            rows.append(
                f"  • {dim}={key}: {s['hit_rate']:.0f}% hit-rate, "
                f"net {100 * s['mean_net']:+.0f}%{edge} (n={s['n']})"
            )
    if not rows:
        return ""
    return (
        "## YOUR TRACK RECORD (paper, net of costs)\n"
        "Calibrate confidence against your realized performance below. This is "
        "context for self-correction, not a rule change.\n" + "\n".join(rows)
    )
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/test_validation.py -k prompt_block -q`
Expected: PASS.

- [ ] **Step 5: Add the param to `build_daily_prompt`.** Change the signature to add `perf_block: str` after `portfolio`:

```python
def build_daily_prompt(
    feed_content: str,
    web_content: str,
    chroma_context: str,
    yesterday_brief: str,
    weekly_summary: str,
    fb: dict,
    portfolio: str,
    perf_block: str = "",
) -> str:
```

Then inject it into the returned template — change the line `{yesterday_block}{weekly_block}{portfolio_block}` to:

```python
{yesterday_block}{weekly_block}{portfolio_block}
{perf_block}
```

- [ ] **Step 6: Build + pass the block in `mode_submit`.** Add the import name to brief's `from validation import (...)` block: `performance_prompt_block,`. Then in `mode_submit`, before the `prompt = build_daily_prompt(` call, add:

```python
    perf_block = performance_prompt_block(load_book())
```

and add `perf_block,` as the final argument to the `build_daily_prompt(...)` call.

- [ ] **Step 7: Run + smoke** — Run: `pytest tests/test_validation.py tests/test_signals.py -q` then `python -c "import brief"`.
Expected: PASS; import clean.

- [ ] **Step 8: Commit**

```bash
git add validation.py brief.py tests/test_validation.py
git commit -m "feat: performance-feedback prompt block injected into daily prompt"
```

---

## Task 8: Unified daily trade message (`mode_collect`)

**Files:**
- Modify: `validation.py` — add `daily_trade_message`.
- Modify: `brief.py` — `mode_collect` sends it after `mode_paper()`.
- Test: `tests/test_validation.py`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_validation.py`:

```python
def test_daily_trade_message_empty_when_nothing():
    assert validation.daily_trade_message({"positions": []}, "2026-06-14") == ""


def test_daily_trade_message_opened_and_open():
    book = {"positions": [
        {"status": "open", "opened": "2026-06-14", "asset_class": "equity",
         "ticker": "SHEL", "direction": "bullish", "play_type": None,
         "entry_price": 30.0, "last_mark": None},
        {"status": "open", "opened": "2026-06-14", "asset_class": "prediction",
         "ticker": "mkt1", "direction": "bullish", "play_type": "momentum",
         "outcome": "Yes", "entry_price": 0.4, "last_mark": None,
         "rationale": "matched (similarity=0.7)"},
        {"status": "open", "opened": "2026-05-01", "asset_class": "crypto",
         "ticker": "BTC", "direction": "bullish", "play_type": None,
         "entry_price": 60000.0,
         "last_mark": {"date": "2026-06-08", "price": 66000.0, "return": 0.10}},
    ]}
    out = validation.daily_trade_message(book, "2026-06-14")
    assert "SHEL" in out          # opened today
    assert "mkt1" in out          # prediction suggestion
    assert "BTC" in out           # open-positions summary
    assert "+10" in out           # last-known mark for the older open position
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/test_validation.py -k trade_message -q`
Expected: FAIL (`AttributeError: ... 'daily_trade_message'`).

- [ ] **Step 3: Implement** — append to `validation.py`:

```python
def daily_trade_message(book: dict, today: str) -> str:
    """Unified daily trade message (Telegram-HTML). Pure — uses last-known marks.

    Three sections, each omitted when empty; returns "" when there is nothing to
    say. Marks are last-known (refreshed by the weekly mark-to-market), not re-priced
    here, to keep the collect path light.
    """
    positions = book.get("positions", [])
    opened = [p for p in positions if p.get("opened") == today]
    open_now = [p for p in positions if p.get("status") == "open"]
    opened_dir = [p for p in opened if p.get("asset_class") != "prediction"]
    opened_pred = [p for p in opened if p.get("asset_class") == "prediction"]
    if not (opened or open_now):
        return ""

    lines = ["<b>📈 TRADE UPDATE</b>"]
    if opened_dir:
        lines.append("<b>Opened today</b>")
        for p in opened_dir:
            lines.append(
                f"  • {p['ticker']} ({p['asset_class']}) {p['direction']} "
                f"@ {p['entry_price']:g}"
            )
    if opened_pred:
        lines.append("<b>Prediction suggestions</b>")
        for p in opened_pred:
            lines.append(
                f"  • {p['ticker']} {p.get('outcome', '')} · {p.get('play_type', '')} "
                f"· {p.get('rationale', '')}"
            )
    if open_now:
        lines.append(f"<b>Open positions ({len(open_now)})</b>")
        for p in open_now:
            mark = p.get("last_mark")
            mstr = f"{100 * mark['return']:+.1f}%" if mark else "—"
            lines.append(f"  • {p['ticker']} ({p['asset_class']}) {p['direction']}: {mstr}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/test_validation.py -k trade_message -q`
Expected: PASS.

- [ ] **Step 5: Wire into `mode_collect`.** Add `daily_trade_message,` to brief's `from validation import (...)` block. Then inside the existing `try:` block in `mode_collect`, **after** `mode_paper()`:

```python
            book = load_book()
            msg = daily_trade_message(book, today)
            if msg:
                telegram_send(msg)
```

(Still inside the `try/except` that runs after `clear_batch_state`, so a Telegram/message failure cannot duplicate the brief — the Phase-3 isolation invariant holds.)

- [ ] **Step 6: Run + smoke** — Run: `pytest tests/test_validation.py -q` then `python -c "import brief"`.
Expected: PASS; import clean.

- [ ] **Step 7: Commit**

```bash
git add validation.py brief.py tests/test_validation.py
git commit -m "feat: unified daily trade message in mode_collect"
```

---

## Task 9: Docs + full gate

**Files:**
- Modify: `README.md` (paper-trading section + state-files tree).

- [ ] **Step 1: Update `README.md`.** In the paper-trading / weekly description, note that the weekly job now posts a **performance report** (hit-rate, net return and edge by asset_class/confidence/play_type/thesis_ref, plus the per-asset go-live gate status) instead of the basic scorecard, and that `collect` posts a **daily trade update** (opened today, prediction suggestions, open positions). Add `paper/gate_history.json` to the `DATA_DIR` tree. Mention the new env knobs (`HAIRCUT_BPS_*`, `GATE_*`) in the configuration section.

- [ ] **Step 2: Run the full pre-push gate** (per [[brief-local-run]]) — Run (PowerShell):

```
ruff check . ; ruff format --check . ; pytest -q
```

Expected: ruff clean, format clean, all tests pass. If `ruff format --check` reports files, run `ruff format .` and re-stage. Inspect pytest output for `FAILED`/`error` markers — do not infer success from a piped exit code.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: phase-4 performance report, daily trade update, new env knobs"
```

- [ ] **Step 4: Push** (solo repo, straight to main per [[newsbrief-commit-to-main]])

```bash
git push origin main
```

---

## Self-review (completed during planning)

**Spec coverage:** data store = book.json (Tasks 3-8 read it; no performance.json) ✓ · benchmark per asset class stamped at open/close (Tasks 2-3) ✓ · config-bps + real prediction orderbook haircut (Tasks 1-3) ✓ · dimensional aggregation (Task 4) ✓ · go-live gate + gate_history.json sustained window (Task 5) ✓ · enhanced weekly report supersedes paper_scorecard (Task 6) ✓ · performance-feedback prompt block, n≥5 (Task 7) ✓ · unified daily trade message (Task 8) ✓ · failure-isolation preserved (Task 8 inside post-clear try/except) ✓ · testing approach (per-task TDD + Task 9 gate) ✓ · Phase-5 items (volume monitor, new commands, LiveExecutor, auto-tuning) absent from all tasks ✓.

**Placeholder scan:** no TBD/TODO; every code step shows full code; commands have expected output.

**Type consistency:** `aggregate_performance` returns `{"overall": stats|None, "dimensions": {dim: {key: stats}}}`; `_stats` keys (`n`, `hit_rate`, `mean_net`, `median_net`, `mean_edge`, `n_edge`) are consumed identically in `evaluate_gate`, `record_gate_history`, `performance_report`, `performance_prompt_block`. Position fields stamped at open (`benchmark_entry`, `entry_spread`) and close (`haircut`, `net_return`, `benchmark_return`, `edge`) match across `trading.py` writers and `validation.py` readers. `build_daily_prompt`'s new `perf_block` param is passed positionally last in `mode_submit`.

**Known execution-time risk:** `_fetch_pg_half_spread` assumes the orderbook JSON exposes `bids`/`asks` lists of `{"price": ...}` (Polymarket-style, which PolyGram proxies). If the live shape differs, only that parser needs adjustment; it is best-effort (returns None → config-bps fallback) so a mismatch degrades gracefully rather than breaking a close.
