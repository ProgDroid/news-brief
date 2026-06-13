# Phase 2: Unified Polymorphic Book + Crypto/Kraken — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the equity-only `paper-book.json` with one polymorphic `book.json`, introduce asset-class dispatch for the resolver/pricer seams, and slot in **crypto via Kraken** as the second paper-traded asset class — preserving all existing equity behaviour and accumulated paper history.

**Architecture:** Crypto reuses the *entire* equity lifecycle (`_signal_return`, the 1w/2w/4w horizons, close at 4w). Only two seams differ — **resolver** (signal ticker → venue instrument) and **pricer** (instrument → mark). A tiny dispatch (`fetch_price(asset_class, instrument)` + `price_position(p)`) routes by `asset_class`; equity → Stooq, crypto → Kraken public Ticker. Positions become polymorphic (`asset_class`/`venue`/`execution`/`instrument`/`play_type` + existing lifecycle fields), and a one-time migration stamps legacy equity positions into the new shape. Crypto resolution is a static majors map (`BTC→XBT`, USD quote) plus an optional `crypto_ticker_map.json` override file — no catalogue API call.

**Tech Stack:** Python 3.12 (CI/Docker) / 3.14 (local), `requests`, `pytest`, `ruff`. No new dependencies. New persisted state under `DATA_DIR`: `book.json`, `crypto_ticker_map.json` (both `_load_json_or`/`_write_json_atomic`).

**Decisions locked in brainstorming (2026-06-13):**
- **Migrate in place** — preserve existing paper history; leave `paper-book.json` as backup.
- **Static crypto majors map + override file** (not a dynamic Kraken AssetPairs catalogue).
- **Rename `stooq_symbol` → `instrument`** (generic resolved-symbol field), migration handles back-compat.
- **`asset_class` defaults to `equity`** on missing/unknown (back-compatible), validated to `equity|crypto`.

**Governing spec:** `docs/superpowers/specs/2026-06-13-multi-asset-trading-polygram-design.md` (sections: Module split, Unified position model, Four pluggable seams, Signal sourcing & prompt change). Prediction/PolyGram (Phase 3), dimensional validation + unified trade message (Phase 4), volume monitor + new Telegram commands (Phase 5) are **out of scope here**.

**Testing convention (from `multi-asset-trading-build` memory):** when a test *monkeypatches* behaviour, patch it on the module whose function is *under test* (`trading.fetch_kraken_price`, `trading.BOOK_FILE`), because each module's functions resolve their own module-level names. When a test just *calls a pure function*, either namespace works.

**Pre-push gate (must match CI exactly — from `brief-local-run` memory):** all THREE, run via the **PowerShell tool** (the Bash tool errors `stdin is not a tty` on python):
- `python -m ruff check brief.py common.py trading.py tests`
- `python -m ruff format --check brief.py common.py trading.py tests`
- `python -m pytest tests -q`

`ruff format` edits files in place — after running it you MUST `git add` every reformatted file (an unstaged reformat passes local `--check` but fails CI on the committed tree). Deps: `pip install -r requirements.txt -r requirements-dev.txt` (ruff pinned 0.14.14).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `trading.py` | Unified book (file + migration + polymorphic shape), Stooq + Kraken resolvers, Stooq + Kraken pricers, `fetch_price`/`price_position` dispatch, lifecycle (`mode_paper`, `mark_to_market`, `_close_position_at_market`), scorecard | Modify |
| `brief.py` | Repoint `trading` imports + `/close` to `load_book`/`save_book`; `normalize_signals` adds `asset_class`; `SYSTEM_PROMPT` + `build_daily_prompt` permit/emit crypto | Modify |
| `tests/test_trading.py` | Smoke list + default-shape; **migration** test | Modify |
| `tests/test_signals.py` | `asset_class` default/validate/passthrough; field-set | Modify |
| `tests/test_crypto.py` (new) | `resolve_kraken_pair`, `fetch_kraken_price`, `fetch_price`/`price_position` dispatch, crypto open in `mode_paper` | Create |

No `Dockerfile` change: no new module (crypto lives in `trading.py`); new state files live under `DATA_DIR`, not in the image.

---

## Task 0: Baseline green

**Files:** none

- [ ] **Step 1: Confirm the suite is green before touching anything**

Run (PowerShell tool): `python -m pytest tests -q`
Expected: PASS. Record the passing count (e.g. "N passed"). Every later task keeps this green; new tests only add to it. If red, STOP and report.

- [ ] **Step 2: Confirm ruff is clean on the baseline**

Run: `python -m ruff check brief.py common.py trading.py tests` then `python -m ruff format --check brief.py common.py trading.py tests`
Expected: both report no issues.

---

## Task 1: Unify the book (file rename + migration + polymorphic position shape)

Replace the equity-only `paper-book.json` concept with one `book.json` of polymorphic positions, migrate legacy positions in place, rename the position field `stooq_symbol → instrument`, and introduce an (equity-only for now) `fetch_price`/`price_position` dispatch that the lifecycle uses instead of calling `fetch_stooq_price` directly.

**Files:**
- Modify: `trading.py` (constants, `load_book`/`save_book` + migration, `fetch_price`/`price_position`, `_close_position_at_market`, `mode_paper`, `mark_to_market`)
- Modify: `brief.py` (import block `trading.py:53-61`; `/close` at `brief.py:351-369`; `mode_weekly` at `brief.py:1369-1373`)
- Modify: `tests/test_trading.py`

- [ ] **Step 1: Write the failing migration + default-shape tests**

In `tests/test_trading.py`, replace the body of `test_paper_book_load_default_shape` and add a migration test. Replace lines 20-24 (the existing `test_paper_book_load_default_shape`) with:

```python
def test_book_load_default_shape(tmp_path, monkeypatch):
    # load_book returns the empty-book shape when neither file exists
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    assert trading.load_book() == {"positions": []}


def test_legacy_book_migrates_in_place(tmp_path, monkeypatch):
    book_file = tmp_path / "book.json"
    legacy_file = tmp_path / "paper-book.json"
    monkeypatch.setattr(trading, "BOOK_FILE", book_file)
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", legacy_file)
    # A legacy equity position: old shape with stooq_symbol, no asset_class.
    legacy_file.write_text(
        '{"positions": [{"ticker": "AAPL", "stooq_symbol": "aapl.us", '
        '"direction": "bullish", "status": "open"}]}',
        encoding="utf-8",
    )
    book = trading.load_book()
    p = book["positions"][0]
    assert p["asset_class"] == "equity"
    assert p["venue"] == "t212"
    assert p["execution"] == "paper"
    assert p["instrument"] == "aapl.us"  # renamed from stooq_symbol
    assert p["play_type"] is None
    assert "stooq_symbol" not in p
    assert book_file.exists()  # migrated copy written
    assert legacy_file.exists()  # original kept as backup
```

Also update `test_trading_exposes_equity_paper_layer` (lines 6-17): replace `"load_paper_book"` and `"save_paper_book"` with `"load_book"`, `"save_book"`, and add `"fetch_price"`, `"price_position"`:

```python
def test_trading_exposes_equity_paper_layer():
    for name in (
        "resolve_stooq_symbol",
        "fetch_stooq_price",
        "fetch_price",
        "price_position",
        "_signal_return",
        "mode_paper",
        "mark_to_market",
        "paper_scorecard",
        "load_book",
        "save_book",
    ):
        assert hasattr(trading, name), name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_trading.py -q`
Expected: FAIL — `AttributeError`/`hasattr` on `load_book`/`save_book`/`fetch_price`/`price_position` and the migration test erroring (`load_book` undefined).

- [ ] **Step 3: Update `trading.py` constants (book file + crypto map paths)**

In `trading.py`, replace the constants block at lines 23-26:

```python
PAPER_DIR = DATA_DIR / "paper"
PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"
```

with:

```python
PAPER_DIR = DATA_DIR / "paper"
BOOK_FILE = PAPER_DIR / "book.json"
# Legacy equity-only book; read once and migrated into BOOK_FILE on first load.
LEGACY_PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"
CRYPTO_TICKER_MAP_FILE = PAPER_DIR / "crypto_ticker_map.json"

# Asset-class → informational venue tag stamped on opened positions.
_VENUE_BY_ASSET = {"equity": "t212", "crypto": "kraken"}
```

- [ ] **Step 4: Replace `load_paper_book`/`save_paper_book` with `load_book`/`save_book` + migration**

In `trading.py`, replace the block at lines 196-201:

```python
def load_paper_book() -> dict:
    return _load_json_or(PAPER_BOOK_FILE, {"positions": []})


def save_paper_book(book: dict):
    _write_json_atomic(PAPER_BOOK_FILE, book)
```

with:

```python
def _migrate_position(p: dict) -> dict:
    """Stamp a legacy equity position into the polymorphic shape (idempotent)."""
    p.setdefault("asset_class", "equity")
    p.setdefault("venue", "t212")
    p.setdefault("execution", "paper")
    p.setdefault("play_type", None)
    if "instrument" not in p:
        p["instrument"] = p.pop("stooq_symbol", None)
    return p


def load_book() -> dict:
    """Load the unified position book, migrating the legacy equity book once.

    If book.json exists it is authoritative. Otherwise, if the legacy
    paper-book.json exists, its positions are stamped into the polymorphic
    shape (asset_class/venue/execution/instrument/play_type) and written to
    book.json; the legacy file is left in place as a backup. A missing pair
    yields the empty-book shape.
    """
    if BOOK_FILE.exists():
        return _load_json_or(BOOK_FILE, {"positions": []})
    legacy = _load_json_or(LEGACY_PAPER_BOOK_FILE, None)
    if legacy is None:
        return {"positions": []}
    legacy["positions"] = [_migrate_position(p) for p in legacy.get("positions", [])]
    _write_json_atomic(BOOK_FILE, legacy)
    log.info(f"Migrated {len(legacy['positions'])} position(s) to book.json")
    return legacy


def save_book(book: dict):
    _write_json_atomic(BOOK_FILE, book)
```

- [ ] **Step 5: Add the (equity-only) price dispatch**

In `trading.py`, immediately AFTER `fetch_stooq_price` (after line 76, before `load_ticker_overrides`), insert:

```python
def fetch_price(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument to market via the pricer for its asset class.

    Equity → Stooq. (The crypto → Kraken branch is added with the Kraken
    pricer.) Returns None on any pricing failure — callers skip, never guess.
    """
    return fetch_stooq_price(instrument)


def price_position(p: dict) -> float | None:
    """Mark a position to market by dispatching on its asset_class."""
    return fetch_price(p.get("asset_class", "equity"), p["instrument"])
```

- [ ] **Step 6: Repoint the lifecycle to `price_position` / `instrument`**

In `trading.py`, in `_close_position_at_market` replace line 216:

```python
    price = fetch_stooq_price(p["stooq_symbol"])
```
with:
```python
    price = price_position(p)
```

In `mark_to_market`, replace lines 340-344:

```python
        price = fetch_stooq_price(p["stooq_symbol"])
        if price is None:
            log.warning(
                f"MtM kept open (no price): {p['ticker']} ({p['stooq_symbol']})"
            )
```
with:
```python
        price = price_position(p)
        if price is None:
            log.warning(
                f"MtM kept open (no price): {p['ticker']} ({p['instrument']})"
            )
```

- [ ] **Step 7: Rewrite `mode_paper` to load the unified book and write the polymorphic shape**

In `trading.py`, replace the entire `mode_paper` function (lines 228-326) with the version below. Changes vs. the original: `load_book`/`save_book`; dedup/reversal keys are 3-tuples `(asset_class, ticker, direction)`; resolution is equity-only here (crypto branch added in Task 5); the opened position carries the polymorphic fields and `instrument` (not `stooq_symbol`).

```python
def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Each medium/high-confidence directional signal with a resolvable instrument opens one
    notional paper position (deduped per asset_class+ticker+direction). Equity prices come
    from Stooq; unmappable tickers, pricing failures, and macro/null-ticker signals are
    skipped and logged. Marking-to-market and closing happen in the weekly job.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return

    signals = json.loads(snap_path.read_text()).get("signals", [])
    actionable = [
        s
        for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("confidence") in ("medium", "high")
        and s.get("ticker")
    ]
    if not actionable:
        log.info("No actionable signals today")
        return

    book = load_book()

    def _open_keys() -> set:
        return {
            (p.get("asset_class", "equity"), p["ticker"], p["direction"])
            for p in book["positions"]
            if p["status"] == "open"
        }

    open_keys = _open_keys()
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()

    opened = 0
    for s in actionable:
        ac = s.get("asset_class", "equity")
        ticker, direction = s["ticker"], s["direction"]
        opposite = "bearish" if direction == "bullish" else "bullish"

        # Reversal: a fresh opposite-direction call closes the standing position first.
        if (ac, ticker, opposite) in open_keys:
            for p in book["positions"]:
                if (
                    p["status"] == "open"
                    and p.get("asset_class", "equity") == ac
                    and p["ticker"] == ticker
                    and p["direction"] == opposite
                    and _close_position_at_market(p, today, "reversal")
                ):
                    log.info(f"Paper reversal: closed {ac} {ticker} {opposite}")
            open_keys = _open_keys()
            if (ac, ticker, opposite) in open_keys:
                # Reversal close couldn't be priced — don't open the opposite yet.
                log.warning(
                    f"Paper skip: unpriced reversal for {ticker}; not opening {direction}"
                )
                continue

        if (ac, ticker, direction) in open_keys:
            continue  # dedup: a position for this call is already open
        symbol = resolve_stooq_symbol(ticker, cache, overrides) if ac == "equity" else None
        if not symbol:
            log.warning(f"Paper skip: no instrument for {ticker} ({ac})")
            continue
        price = fetch_price(ac, symbol)
        if price is None:
            log.warning(f"Paper skip: no price for {ticker} ({symbol})")
            continue
        book["positions"].append(
            {
                "id": f"{today}:{ac}:{ticker}:{direction}",
                "opened": today,
                "asset_class": ac,
                "venue": _VENUE_BY_ASSET.get(ac, ""),
                "execution": "paper",
                "ticker": ticker,
                "instrument": symbol,
                "play_type": None,
                "direction": direction,
                "confidence": s.get("confidence"),
                "topic": s.get("topic"),
                "thesis_ref": s.get("thesis_ref"),
                "rationale": s.get("rationale"),
                "entry_price": price,
                "entry_date": today,
                "status": "open",
                "close_reason": None,
                "closed_date": None,
                "checkpoints": {},
                "last_mark": None,
                "realized_return": None,
            }
        )
        open_keys.add((ac, ticker, direction))
        opened += 1

    save_book(book)
    log.info(f"Opened {opened} paper position(s)")
```

- [ ] **Step 8: Repoint `brief.py` imports + call sites**

In `brief.py`, in the `from trading import (...)` block (lines 53-61), replace `load_paper_book,` and `save_paper_book,` with `load_book,` and `save_book,`.

In `/close` (lines 351-369), replace `load_paper_book()` → `load_book()` (line 353) and `save_paper_book(book)` → `save_book(book)` (line 363).

In `mode_weekly` (lines 1369-1373), replace:
```python
    book = mark_to_market(
        load_paper_book(), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    save_paper_book(book)
```
with:
```python
    book = mark_to_market(
        load_book(), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    save_book(book)
```

- [ ] **Step 9: Verify clean import + run the suite**

Run: `python -c "import brief, trading, common; print('ok')"`
Expected: prints `ok` (no `ImportError`/`NameError`). If a `NameError` for `load_paper_book`/`save_paper_book` surfaces, a call site was missed — grep `python -m ... ` is not needed; search `brief.py` for the old names.

Run: `python -m pytest tests -q`
Expected: PASS — Task 0 count + 1 (the new migration test; the renamed default-shape test replaces the old one).

- [ ] **Step 10: ruff + commit**

Run: `python -m ruff check brief.py common.py trading.py tests` and `python -m ruff format brief.py common.py trading.py tests`. Then `git status` to see every reformatted file.

```bash
git add trading.py brief.py tests/test_trading.py
git commit -m "feat: unify paper book into polymorphic book.json with legacy migration"
```

---

## Task 2: Signal schema — `asset_class` field + prompt

Add `asset_class` to the normalized signal schema (default `equity`, validate `equity|crypto`) and update the model-facing prompt to permit crypto directional calls and emit the field.

**Files:**
- Modify: `brief.py` (`normalize_signals` ~lines 986-1018; `SYSTEM_PROMPT` lines 710-722; emitted schema in `build_daily_prompt` lines 831-842)
- Modify: `tests/test_signals.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_signals.py`, add the `asset_class` cases and extend the field-set assertion. Append after `test_normalize_nulls_nullish_ticker_and_thesis` (after line 105):

```python
def test_normalize_defaults_asset_class_to_equity():
    s = dict(SIGNAL)  # no asset_class key
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "equity"


def test_normalize_keeps_valid_crypto_asset_class():
    s = dict(SIGNAL, ticker="BTC", asset_class="crypto")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "crypto"


def test_normalize_unknown_asset_class_falls_back_to_equity():
    s = dict(SIGNAL, asset_class="forex")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["asset_class"] == "equity"
```

And update `test_normalize_strips_unknown_fields` (lines 108-119) so the expected set includes `"asset_class"`:

```python
def test_normalize_strips_unknown_fields():
    s = dict(SIGNAL, price_target=120, note="extra")
    clean, _ = brief.normalize_signals([s])
    assert set(clean[0]) == {
        "ticker",
        "topic",
        "direction",
        "confidence",
        "thesis_ref",
        "rationale",
        "provenance",
        "asset_class",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_signals.py -q`
Expected: FAIL — `KeyError: 'asset_class'` (default test) and the field-set test failing (set lacks `asset_class`).

- [ ] **Step 3: Add the `asset_class` enum + normalize it**

In `brief.py`, add the valid set next to the other synonym maps. After `_NULLISH = {...}` (line 976), add:

```python
_ASSET_CLASSES = {"equity", "crypto"}
```

In `normalize_signals`, in the appended dict (lines 1007-1016), add the field (place it after `"provenance"`):

```python
        clean.append(
            {
                "ticker": _nullish(item.get("ticker")),
                "topic": topic,
                "direction": direction,
                "confidence": confidence,
                "thesis_ref": _nullish(item.get("thesis_ref")),
                "rationale": str(item.get("rationale", "")).strip(),
                "provenance": str(item.get("provenance", "")).strip(),
                "asset_class": (
                    ac
                    if (ac := str(item.get("asset_class", "")).strip().lower())
                    in _ASSET_CLASSES
                    else "equity"
                ),
            }
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_signals.py -q`
Expected: PASS (all signal tests, including the three new ones).

- [ ] **Step 5: Update the prompt to permit + emit crypto**

In `brief.py`, in `SYSTEM_PROMPT` (lines 710-722), add a line to the reader bullet list. After the line `- Prefers Reuters as a primary news source` (line 718), add:

```
- Trades equities and major cryptocurrencies (BTC, ETH, and other large-cap coins); surface directional crypto calls the same way as equities when news warrants
```

In `build_daily_prompt`, update the emitted JSON schema (lines 832-842). Replace the `ticker` line and add an `asset_class` line so the schema block reads:

```python
    {{
        "ticker": "the primary listing symbol — e.g. SHEL or BP for equities, BTC or ETH for crypto; null only for macro-level signals with no single tradable instrument",
        "asset_class": "equity | crypto — equity for stocks/ETFs, crypto for major coins; default to equity if unsure",
        "topic": "short topic label, e.g. hormuz-disruption",
        "direction": "bullish | bearish | neutral",
        "thesis_ref": "the held thesis this bears on, or null",
        "confidence": "low | medium | high",
        "rationale": "one sentence, no more",
        "provenance": "which source/feed/search this came from"
    }}
```

(This is inside the triple-quoted f-string; keep the doubled braces `{{ }}` exactly.)

- [ ] **Step 6: Verify import + full suite**

Run: `python -c "import brief"` (Expected: clean) then `python -m pytest tests -q` (Expected: PASS, Task 1 count + 3).

- [ ] **Step 7: ruff + commit**

Run: `python -m ruff check brief.py common.py trading.py tests` and `python -m ruff format brief.py common.py trading.py tests`; `git status`.

```bash
git add brief.py tests/test_signals.py
git commit -m "feat: add asset_class to signal schema and permit crypto calls in prompt"
```

---

## Task 3: Crypto resolver (`resolve_kraken_pair`)

Static majors map (`BTC→XBT`, USD quote) + optional `crypto_ticker_map.json` override. Pure function, no network.

**Files:**
- Modify: `trading.py` (constants + `resolve_kraken_pair` + `load_crypto_ticker_overrides`)
- Create: `tests/test_crypto.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_crypto.py`:

```python
"""Crypto seam: Kraken pair resolution + pricing + asset-class dispatch."""

import trading


# ── resolve_kraken_pair ───────────────────────────────────────────────────────
def test_btc_maps_to_xbt_usd_pair():
    assert trading.resolve_kraken_pair("BTC", {}) == "XBTUSD"


def test_eth_maps_to_eth_usd_pair():
    assert trading.resolve_kraken_pair("ETH", {}) == "ETHUSD"


def test_resolve_kraken_is_case_insensitive():
    assert trading.resolve_kraken_pair("btc", {}) == "XBTUSD"


def test_crypto_override_is_authoritative():
    assert trading.resolve_kraken_pair("FOO", {"FOO": "FOOUSD"}) == "FOOUSD"


def test_unknown_coin_returns_none():
    assert trading.resolve_kraken_pair("NOTACOIN", {}) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_crypto.py -q`
Expected: FAIL — `AttributeError: module 'trading' has no attribute 'resolve_kraken_pair'`.

- [ ] **Step 3: Add the map + resolver**

In `trading.py`, after the `_VENUE_BY_ASSET` constant (added in Task 1), add:

```python
# Crypto majors → Kraken base asset (encodes Kraken's BTC→XBT, DOGE→XDG quirks).
# Signals carry plain symbols (BTC, ETH); the realistic tradable universe is small
# and stable, so a static map + an optional crypto_ticker_map.json override beats a
# catalogue fetch. Unlisted/exotic coins resolve to None (skipped + logged).
_KRAKEN_QUOTE = "USD"
_KRAKEN_BASE = {
    "BTC": "XBT",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "ADA": "ADA",
    "DOT": "DOT",
    "LINK": "LINK",
    "LTC": "LTC",
    "DOGE": "XDG",
    "AVAX": "AVAX",
}
```

Then add the loader + resolver near `resolve_stooq_symbol` (after `resolve_stooq_symbol`, before `load_paper_book`/`load_book`):

```python
def load_crypto_ticker_overrides() -> dict:
    """Manual signal-ticker -> Kraken-pair overrides (mirrors load_ticker_overrides)."""
    return _load_json_or(CRYPTO_TICKER_MAP_FILE, {})


def resolve_kraken_pair(ticker: str, overrides: dict) -> str | None:
    """Map a signal crypto ticker (BTC, ETH, ...) to a Kraken USD pair (e.g. 'XBTUSD').

    Override file is authoritative; otherwise the static majors map supplies the base
    asset and the USD quote is appended. Returns None for unmapped tickers — callers
    skip and log (same posture as an unresolvable equity).
    """
    if ticker in overrides:
        return overrides[ticker]
    base = _KRAKEN_BASE.get(ticker.strip().upper())
    if base is None:
        return None
    return f"{base}{_KRAKEN_QUOTE}"
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_crypto.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: ruff + commit**

Run: `python -m ruff check ... tests` + `python -m ruff format ... tests`; `git status`.

```bash
git add trading.py tests/test_crypto.py
git commit -m "feat: add Kraken crypto resolver (static majors map + override)"
```

---

## Task 4: Crypto pricer (`fetch_kraken_price`) + dispatch crypto branch

Kraken public Ticker, last-trade close, None-on-failure posture mirroring `fetch_stooq_price`. Then extend `fetch_price` to route crypto to it.

**Files:**
- Modify: `trading.py` (`fetch_kraken_price` + extend `fetch_price`)
- Modify: `tests/test_crypto.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_crypto.py`, append:

```python
# ── fetch_kraken_price ────────────────────────────────────────────────────────
class _FakeKrakenResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_kraken(monkeypatch, payload):
    monkeypatch.setattr(
        trading.requests, "get", lambda url, timeout=None: _FakeKrakenResp(payload)
    )


def test_kraken_parses_last_trade_close(monkeypatch):
    # Kraken returns the canonical pair key (XXBTZUSD) regardless of the queried alias;
    # 'c' is [last_trade_price, lot_volume].
    _patch_kraken(monkeypatch, {"error": [], "result": {"XXBTZUSD": {"c": ["63000.5", "0.01"]}}})
    assert trading.fetch_kraken_price("XBTUSD") == 63000.5


def test_kraken_error_array_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": ["EQuery:Unknown asset pair"], "result": {}})
    assert trading.fetch_kraken_price("NOPEUSD") is None


def test_kraken_empty_result_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": [], "result": {}})
    assert trading.fetch_kraken_price("XBTUSD") is None


def test_kraken_non_positive_returns_none(monkeypatch):
    _patch_kraken(monkeypatch, {"error": [], "result": {"XXBTZUSD": {"c": ["0", "0"]}}})
    assert trading.fetch_kraken_price("XBTUSD") is None


# ── fetch_price / price_position dispatch ─────────────────────────────────────
def test_fetch_price_routes_by_asset_class(monkeypatch):
    monkeypatch.setattr(trading, "fetch_stooq_price", lambda s: ("stooq", s))
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda s: ("kraken", s))
    assert trading.fetch_price("equity", "aapl.us") == ("stooq", "aapl.us")
    assert trading.fetch_price("crypto", "XBTUSD") == ("kraken", "XBTUSD")


def test_price_position_dispatches_on_asset_class(monkeypatch):
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda s: 100.0)
    p = {"asset_class": "crypto", "instrument": "XBTUSD"}
    assert trading.price_position(p) == 100.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_crypto.py -q`
Expected: FAIL — `fetch_kraken_price` undefined; `test_fetch_price_routes_by_asset_class` fails because `fetch_price` ignores `asset_class` and always calls Stooq.

- [ ] **Step 3: Add `fetch_kraken_price`**

In `trading.py`, immediately after `fetch_stooq_price` (and after the Task-1 `fetch_price`/`price_position`, placement is fine either way as long as it's module-level), add:

```python
def fetch_kraken_price(pair: str) -> float | None:
    """Fetch the last-trade price for a Kraken pair (e.g. 'XBTUSD') via the public API.

    Returns None on network error, a non-empty Kraken error array, an empty/garbled
    result, or a non-positive price — callers MUST treat None as 'could not price' and
    skip, never substitute a guessed value (mirrors fetch_stooq_price).
    """
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        log.warning(f"Kraken error for {pair}: {data['error']}")
        return None
    result = data.get("result") or {}
    if not result:
        log.warning(f"Kraken returned no result for {pair}")
        return None
    # Kraken keys the result by its canonical pair name (e.g. XXBTZUSD), not the
    # queried alias — take the single entry rather than indexing by `pair`.
    entry = next(iter(result.values()))
    try:
        price = float(entry["c"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        log.warning(f"Kraken price unparseable for {pair}")
        return None
    if price <= 0:
        log.warning(f"Kraken returned non-positive price for {pair}: {price}")
        return None
    return price
```

- [ ] **Step 4: Extend `fetch_price` with the crypto branch**

In `trading.py`, replace the body of `fetch_price` (added in Task 1) so it dispatches:

```python
def fetch_price(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument to market via the pricer for its asset class.

    Equity → Stooq, crypto → Kraken. Returns None on any pricing failure —
    callers skip, never guess.
    """
    if asset_class == "crypto":
        return fetch_kraken_price(instrument)
    return fetch_stooq_price(instrument)
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_crypto.py -q`
Expected: PASS (all crypto tests).

- [ ] **Step 6: ruff + commit**

```bash
git add trading.py tests/test_crypto.py
git commit -m "feat: add Kraken pricer and route crypto through fetch_price dispatch"
```

---

## Task 5: Wire crypto into the open loop (`mode_paper`)

Make `mode_paper` resolve + price crypto signals through the Kraken seam. This is the only change that lets a crypto position be opened.

**Files:**
- Modify: `trading.py` (`mode_paper` — load crypto overrides; crypto resolution branch)
- Modify: `tests/test_crypto.py`

- [ ] **Step 1: Write the failing test (crypto open)**

In `tests/test_crypto.py`, append an integration test that drives `mode_paper` over a stubbed crypto signal:

```python
# ── mode_paper opens a crypto position ────────────────────────────────────────
def test_mode_paper_opens_crypto_position(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    monkeypatch.setattr(trading, "SIGNALS_DIR", signals_dir)
    monkeypatch.setattr(trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(trading, "LEGACY_PAPER_BOOK_FILE", tmp_path / "paper-book.json")
    # No T212 call; crypto resolution doesn't use the equity cache anyway.
    monkeypatch.setattr(trading, "refresh_instruments_cache", lambda *a, **k: {})
    monkeypatch.setattr(trading, "fetch_kraken_price", lambda pair: 60000.0)

    (signals_dir / f"signals-{today}.json").write_text(
        '{"signals": [{"ticker": "BTC", "asset_class": "crypto", '
        '"direction": "bullish", "confidence": "high", "topic": "btc-etf-flows", '
        '"thesis_ref": null, "rationale": "Inflows accelerating.", "provenance": "web_search"}]}',
        encoding="utf-8",
    )

    trading.mode_paper()

    book = trading.load_book()
    assert len(book["positions"]) == 1
    p = book["positions"][0]
    assert p["asset_class"] == "crypto"
    assert p["venue"] == "kraken"
    assert p["execution"] == "paper"
    assert p["instrument"] == "XBTUSD"
    assert p["entry_price"] == 60000.0
    assert p["status"] == "open"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_crypto.py::test_mode_paper_opens_crypto_position -q`
Expected: FAIL — `mode_paper` resolves crypto to `None` (equity-only branch from Task 1), so it logs "no instrument" and writes zero positions; `len(...) == 1` fails.

- [ ] **Step 3: Add the crypto resolution branch in `mode_paper`**

In `trading.py` `mode_paper`, after `overrides = load_ticker_overrides()`, add the crypto overrides load:

```python
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()
    crypto_overrides = load_crypto_ticker_overrides()
```

Then replace the resolution line:

```python
        symbol = resolve_stooq_symbol(ticker, cache, overrides) if ac == "equity" else None
```
with:
```python
        if ac == "crypto":
            symbol = resolve_kraken_pair(ticker, crypto_overrides)
        else:
            symbol = resolve_stooq_symbol(ticker, cache, overrides)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_crypto.py -q`
Expected: PASS (including the crypto-open test).

- [ ] **Step 5: ruff + commit**

```bash
git add trading.py tests/test_crypto.py
git commit -m "feat: open crypto paper positions from signals via Kraken seam"
```

---

## Task 6: Final verification sweep

**Files:** none (verification only; one commit if ruff reformats)

- [ ] **Step 1: Full pre-push gate**

Run all three, in order:
- `python -m ruff check brief.py common.py trading.py tests` → no issues
- `python -m ruff format --check brief.py common.py trading.py tests` → "N files already formatted"
- `python -m pytest tests -q` → PASS, count = Task 0 baseline + 8 new tests (1 migration + 3 signals + 5 crypto resolver + 6 crypto pricer/dispatch + 1 crypto-open − 0; verify the absolute number is baseline+N where N = total added)

If `ruff format --check` reports a file would change, run `python -m ruff format ...`, `git add` the file(s), and commit `style: apply ruff format`.

- [ ] **Step 2: Confirm no stale `stooq_symbol` position reads remain**

Use Grep for `stooq_symbol` across `*.py`.
Expected: matches ONLY inside `fetch_stooq_price` (its parameter name `stooq_symbol` and the log/url lines) and the migration `p.pop("stooq_symbol", None)`. There must be NO `p["stooq_symbol"]` position access left in `mark_to_market`, `_close_position_at_market`, or `mode_paper`.

- [ ] **Step 3: Confirm no stale `load_paper_book`/`save_paper_book`/`PAPER_BOOK_FILE` references**

Use Grep for `load_paper_book|save_paper_book|PAPER_BOOK_FILE` across `*.py`.
Expected: NO matches (all renamed to `load_book`/`save_book`/`BOOK_FILE`; `LEGACY_PAPER_BOOK_FILE` is the only `*PAPER_BOOK*` name and is fine).

- [ ] **Step 4: Confirm each entrypoint mode still dispatches**

Run: `python -c "import brief; print([m for m in ('mode_submit','mode_collect','mode_weekly','mode_commands','mode_run','mode_paper') if hasattr(brief, m)])"`
Expected: lists all six modes.

- [ ] **Step 5: Final commit (only if Step 1 reformatted anything not yet committed)**

```bash
git add -A
git commit -m "style: ruff format after phase 2"
```

(Skip if nothing changed.)

---

## Self-Review

- **Spec coverage:**
  - *Unified position model* (`book.json`, polymorphic fields, migration) → Task 1.
  - *Field `instrument`* generalising `stooq_symbol` → Task 1.
  - *Four pluggable seams, dispatch by asset_class* → resolver (Task 3 + wired Task 5), pricer (Task 4), `fetch_price`/`price_position` dispatch (Tasks 1+4); return/close trigger shared (no change — equity lifecycle reused, verified by existing `_signal_return`/`mark_to_market` tests staying green).
  - *Crypto via Kraken* (resolver Task 3, pricer Task 4, paper executor = `mode_paper` open Task 5) → covered.
  - *Signal sourcing & prompt change* (`asset_class: equity|crypto`, `normalize_signals` validate/default, `SYSTEM_PROMPT` permits crypto, emitted schema) → Task 2.
  - *Out of scope held:* no PolyGram/prediction, no dimensional validation breakdown, no unified trade message, no volume monitor, no new Telegram commands, no live executor — none appear in any task. ✅
- **Placeholder scan:** No TBD/TODO. Every code step shows complete code; every run step shows the command + expected result. The Task 1 equity-only `fetch_price` is a deliberate, complete intermediate (returns Stooq price), upgraded in Task 4 — not a placeholder.
- **Type/name consistency:** `load_book`/`save_book` (Task 1) used consistently in `brief.py` (Task 1 Step 8) and tests; `BOOK_FILE`/`LEGACY_PAPER_BOOK_FILE`/`CRYPTO_TICKER_MAP_FILE`/`_VENUE_BY_ASSET`/`_KRAKEN_BASE`/`_KRAKEN_QUOTE` defined once. `fetch_price(asset_class, instrument)` signature stable across Tasks 1/4/5; `price_position(p)` reads `p["asset_class"]`/`p["instrument"]`. `resolve_kraken_pair(ticker, overrides)` matches the `resolve_stooq_symbol(ticker, cache, overrides)` shape minus the unused cache. Position dict key `instrument` (not `stooq_symbol`) consistent across migration, `mode_paper` writer, and lifecycle readers.
- **Risk:** Migration is idempotent (runs only when `book.json` is absent) and non-destructive (legacy file kept). The one behaviour-touching equity change is repointing lifecycle reads from `p["stooq_symbol"]` to `price_position(p)`/`p["instrument"]`; covered by the existing `test_paper.py` resolver/pricer/return tests (unchanged) plus the migration test proving old positions read correctly under the new field. Crypto open is gated entirely behind `asset_class == "crypto"`, which only normalized signals can carry, so equity flow is untouched until the model emits a crypto signal.
