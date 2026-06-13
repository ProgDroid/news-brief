# Phase 1: Module Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the shared infrastructure and the equity paper-trading layer out of the 1982-line `brief.py` into `common.py` and `trading.py`, with zero behaviour change, so the multi-asset work (phases 2–5) builds on focused modules.

**Architecture:** One-way dependency `common ← trading ← brief`. `common.py` holds infra used by ≥2 modules (config, paths, logging, JSON I/O, Telegram/HTML, Anthropic headers, T212 auth). `trading.py` holds the equity paper layer. `brief.py` keeps sources, prompts, delivery, command dispatch, and orchestration, importing from both.

**Tech Stack:** Python 3.12, `requests`, `pytest`. No new dependencies.

**Discipline note (refactor, not feature):** This is a pure relocation. The safety net is the **existing test suite staying green** plus new import-smoke tests. Each task moves a cohesive group, fixes imports, runs the full suite, and commits. Moves are specified by **symbol name** (use Serena `find_symbol <name> --include_body` to read the current body and `replace_content`/`insert_*` to relocate) rather than line numbers, which drift after the first edit.

**Environment:**
- Run tests from repo root with the data dir overridden (Windows PowerShell): `$env:NEWSBRIEF_DATA_DIR = "$env:TEMP\nb-test"; python -m pytest tests -q`
- `conftest.py` already sets `NEWSBRIEF_DATA_DIR` before import, so plain `python -m pytest tests -q` also works.
- Baseline: confirm the suite is green before touching anything.

---

## File Structure

| File | Responsibility | Imports |
|---|---|---|
| `common.py` (new) | Config/env, `DATA_DIR` + `SIGNALS_DIR`, logging + `log`, JSON atomic I/O, Telegram + HTML sanitising, Anthropic headers/model, T212 config + auth header | stdlib, `requests` |
| `trading.py` (new) | Equity paper layer: Stooq resolution + pricing, book I/O, return math, position close, `mode_paper`, `mark_to_market`, `paper_scorecard` | `common` |
| `brief.py` (modified) | Sources, prompts, delivery, Telegram command dispatch, batch submit/collect, orchestration | `common`, `trading` |
| `tests/test_common.py` (new) | Import-smoke + relocated infra coverage | `common` |
| `tests/test_paper.py` (modified) | Repoint imports/patches from `brief` → `trading` | `trading` |
| `Dockerfile` (modified) | `COPY common.py trading.py` alongside `brief.py` | — |

---

## Task 0: Baseline green

**Files:** none

- [ ] **Step 1: Run the full suite and confirm it passes**

Run: `python -m pytest tests -q`
Expected: PASS (all tests green). If anything fails, STOP and report — do not refactor on a red baseline.

- [ ] **Step 2: Record the test count**

Note the number of passing tests (e.g. "31 passed"). Every later task must keep this count green (new tests only add to it).

---

## Task 1: Create `common.py` and move shared infrastructure

**Files:**
- Create: `common.py`
- Modify: `brief.py` (remove moved symbols; add `from common import ...`)
- Test: `tests/test_common.py` (create)

**Symbols to move into `common.py`** (read each with Serena `find_symbol <name> --include_body`, paste verbatim, delete from `brief.py`):

- Module imports needed by these symbols: `base64`, `html`, `os`, `re`, `json`, `logging`, `requests`, `from pathlib import Path`.
- Config/env constants: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `REQUIRED_ENV`, `DATA_DIR`.
- Logging: `_log_handlers`, the `logging.basicConfig(...)` call, `log = logging.getLogger("newsbrief")` (rename from `__name__` → the literal `"newsbrief"` so the logger name is stable across modules).
- Anthropic: `ANTHROPIC_HEADERS`, `MODEL`.
- Paths shared by both modules: `SIGNALS_DIR` (keep `STATE_FILE`, `FEEDBACK_FILE`, `BRIEFS_DIR`, `WEEKLY_DIR`, `THESIS_FILE` in `brief.py` — they are brief-only).
- JSON I/O: `_write_json_atomic`, `_load_json_or`.
- Telegram + HTML: `TELEGRAM_MAX_LEN`, `ALLOWED_TAGS`, `_redact`, `telegram_send`, `telegram_alert`, `sanitise_html`, `split_html_message`.
- T212 config + auth (shared by `fetch_portfolio_weights` in brief and `refresh_instruments_cache` in trading): `T212_API_KEY_ID`, `T212_API_KEY`, `T212_BASE_URL`, `t212_auth_header`.

- [ ] **Step 1: Write the failing import-smoke test**

Create `tests/test_common.py`:

```python
"""common.py: shared infra smoke + relocated behaviour."""

import common


def test_common_imports_and_exposes_infra():
    assert callable(common.telegram_send)
    assert callable(common._write_json_atomic)
    assert callable(common._load_json_or)
    assert callable(common.sanitise_html)
    assert callable(common.split_html_message)
    assert callable(common.t212_auth_header)
    assert common.MODEL  # non-empty model id
    assert str(common.SIGNALS_DIR).endswith("signals")


def test_load_json_or_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    assert common._load_json_or(p, {"d": 1}) == {"d": 1}  # missing → default
    common._write_json_atomic(p, {"a": 2})
    assert common._load_json_or(p, None) == {"a": 2}


def test_sanitise_html_strips_disallowed_tags():
    out = common.sanitise_html("<b>ok</b><script>bad()</script>")
    assert "<b>ok</b>" in out
    assert "script" not in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_common.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'common'`.

- [ ] **Step 3: Create `common.py` with the moved symbols**

Create `common.py` with a module docstring and the symbols listed above, in this order: imports → env/config → logging (`log = logging.getLogger("newsbrief")`) → Anthropic → paths (`DATA_DIR`, `SIGNALS_DIR`) → T212 config + `t212_auth_header` → `_write_json_atomic` / `_load_json_or` → `_redact` / `telegram_send` / `telegram_alert` → `TELEGRAM_MAX_LEN` / `ALLOWED_TAGS` / `sanitise_html` / `split_html_message`. Paste each body verbatim from `brief.py`. Header:

```python
#!/usr/bin/env python3
"""Shared infrastructure for newsbrief: config, paths, logging, JSON I/O,
Telegram + HTML, Anthropic headers, and T212 auth. No domain logic; imported
by both brief.py and trading.py (one-way dependency, no cycles)."""
```

- [ ] **Step 4: Update `brief.py` to import from `common` and delete the moved definitions**

Delete the moved symbol definitions from `brief.py`. Near the top of `brief.py` (after stdlib imports), add:

```python
from common import (
    ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, REQUIRED_ENV,
    DATA_DIR, SIGNALS_DIR, log, ANTHROPIC_HEADERS, MODEL,
    T212_API_KEY_ID, T212_API_KEY, T212_BASE_URL, t212_auth_header,
    TELEGRAM_MAX_LEN, ALLOWED_TAGS,
    _write_json_atomic, _load_json_or, _redact,
    telegram_send, telegram_alert, sanitise_html, split_html_message,
)
```

Keep `brief.py`'s own `logging.basicConfig` call removed (logging is configured once, in `common.py`, on first import). Leave `STATE_FILE`, `FEEDBACK_FILE`, `BRIEFS_DIR`, `WEEKLY_DIR`, `THESIS_FILE`, `MAX_TOKENS`, `CHROMA_MCP_URL`, `NITTER_BASE_URL` defined in `brief.py` (brief-only).

- [ ] **Step 5: Find any references that still resolve to the old `brief.` definitions**

Run Serena `find_referencing_symbols` for each moved function, or:
Run: `python -c "import brief"`
Expected: imports cleanly, no `NameError`/`AttributeError`. If a `NameError` appears, add the missing name to the `from common import (...)` block.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS — the Task 0 count plus the 3 new `test_common.py` tests.

- [ ] **Step 7: Commit**

```bash
git add common.py brief.py tests/test_common.py
git commit -m "refactor: extract shared infra into common.py"
```

---

## Task 2: Create `trading.py` and move the equity paper layer

**Files:**
- Create: `trading.py`
- Modify: `brief.py` (import paper-layer symbols from `trading`; delete moved defs)
- Modify: `tests/test_paper.py` (repoint `brief` → `trading`)

**Symbols to move into `trading.py`** (verbatim via Serena):

- Constants: `PAPER_DIR`, `PAPER_BOOK_FILE`, `TICKER_MAP_FILE`, `INSTRUMENTS_CACHE_FILE`, `PAPER_HORIZONS`, `PAPER_CLOSE_HORIZON`, `_STOOQ_SUFFIX`, `_STOOQ_EUR_BY_ISIN`, `_STOOQ_MARKET_MARKER`, `_COUNTRY_PREFERENCE`.
- Functions: `fetch_stooq_price`, `load_ticker_overrides`, `load_instruments_cache`, `refresh_instruments_cache`, `_match_instrument_by_base`, `resolve_stooq_symbol`, `load_paper_book`, `save_paper_book`, `_signal_return`, `_close_position_at_market`, `mode_paper`, `mark_to_market`, `paper_scorecard`.

These call into `common` for: `DATA_DIR`, `SIGNALS_DIR`, `log`, `_write_json_atomic`, `_load_json_or`, `T212_BASE_URL`, `t212_auth_header`. They use `requests`, `json`, `datetime`.

- [ ] **Step 1: Write the failing import-smoke test**

Create `tests/test_trading.py`:

```python
"""trading.py: smoke + that the equity paper layer relocated intact."""

import trading


def test_trading_exposes_equity_paper_layer():
    for name in ("resolve_stooq_symbol", "fetch_stooq_price", "_signal_return",
                 "mode_paper", "mark_to_market", "paper_scorecard",
                 "load_paper_book", "save_paper_book"):
        assert hasattr(trading, name), name


def test_signal_return_directionality():
    assert trading._signal_return("bullish", 100.0, 110.0) == 0.10
    assert trading._signal_return("bearish", 100.0, 110.0) == -0.10
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_trading.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading'`.

- [ ] **Step 3: Create `trading.py` with the moved symbols**

Create `trading.py` with this header and the symbols above (paste bodies verbatim from `brief.py`):

```python
#!/usr/bin/env python3
"""Equity paper-trading layer: Stooq ticker resolution + pricing, the paper
book, return math, position close, and the open/mark-to-market/scorecard
functions. Imports infra from common.py. (Phases 2-5 generalise this to a
multi-asset subsystem.)"""

import json
import requests
from datetime import datetime, timezone

from common import (
    DATA_DIR, SIGNALS_DIR, log, _write_json_atomic, _load_json_or,
    T212_BASE_URL, t212_auth_header,
)

PAPER_DIR = DATA_DIR / "paper"
PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"
```

(Adjust the exact `datetime` import to match what the moved bodies use — check with Serena before deleting from `brief.py`.)

- [ ] **Step 4: Update `brief.py` — import paper symbols from `trading`, delete moved defs**

Delete the moved definitions from `brief.py`. Add:

```python
from trading import (
    load_paper_book, save_paper_book, _close_position_at_market,
    mode_paper, mark_to_market, paper_scorecard,
)
```

(`/close` in `_handle_telegram_update` uses `load_paper_book`, `save_paper_book`, `_close_position_at_market`; `mode_collect`/`mode_run` call `mode_paper`; `mode_weekly` calls `mark_to_market` + `paper_scorecard`. Confirm each call site resolves.)

- [ ] **Step 5: Repoint `tests/test_paper.py` from `brief` to `trading`**

In `tests/test_paper.py`, replace `import brief` with `import trading` and replace every `brief.` with `trading.` (including the monkeypatch target `brief.requests` → `trading.requests` in `_patch_stooq`). The assertions stay byte-for-byte identical — only the module reference changes, proving behaviour is unchanged.

- [ ] **Step 6: Verify clean import**

Run: `python -c "import brief, trading, common"`
Expected: no errors. If `NameError` for a paper symbol appears in `brief`, add it to the `from trading import (...)` block.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS — Task 1 count plus `test_trading.py`'s 2 tests, with `test_paper.py` still green against `trading`.

- [ ] **Step 8: Commit**

```bash
git add trading.py brief.py tests/test_trading.py tests/test_paper.py
git commit -m "refactor: extract equity paper layer into trading.py"
```

---

## Task 3: Update the Docker build for the new modules

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Add the new modules to the COPY layer**

In `Dockerfile`, change:

```dockerfile
COPY brief.py .
```

to:

```dockerfile
COPY common.py trading.py brief.py .
```

- [ ] **Step 2: Verify the image builds and the entrypoint still imports**

Run: `docker build -t newsbrief:phase1 .`
Then: `docker run --rm --entrypoint python newsbrief:phase1 -c "import brief, trading, common; print('ok')"`
Expected: prints `ok` (the modules import inside the image).

(If Docker is unavailable in the working environment, instead run `python -c "import brief, trading, common; print('ok')"` locally and note the Docker verification as a manual follow-up.)

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "build: copy common.py and trading.py into image"
```

---

## Task 4: Final verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Confirm no stale cross-module references remain**

Run: `python -m pytest tests -q`
Expected: PASS, count = Task 0 baseline + 5 new tests.

- [ ] **Step 2: Confirm each entrypoint mode still dispatches**

Run: `python -c "import brief; print([m for m in ('mode_submit','mode_collect','mode_weekly','mode_commands','mode_run','mode_paper') if hasattr(brief, m)])"`
Expected: lists all six modes (`mode_paper` is imported from `trading` and re-exposed via `brief`). If `mode_paper` is missing from the list, confirm it is in the `from trading import` block (it must be, because the `paper` CLI mode dispatches to it).

- [ ] **Step 3: Grep for any symbol still referenced as `brief.<moved>` outside imports**

Use Grep for `brief\.(resolve_stooq_symbol|fetch_stooq_price|_signal_return|telegram_send|_write_json_atomic|sanitise_html)` across `*.py`.
Expected: matches only in `from common import` / `from trading import` lines, nowhere else.

- [ ] **Step 4: Final commit (if Step 2 required adding `mode_paper`/other to an import block)**

```bash
git add brief.py
git commit -m "refactor: re-expose paper CLI mode after extraction"
```

(Skip if nothing changed.)

---

## Self-Review

- **Spec coverage:** Implements spec section "Module split" (common.py + trading.py + brief.py, one-way dependency). Preserves equity behaviour (phases 2–5 build on this). No other spec section is in scope for Phase 1.
- **Placeholders:** none — every move is a named symbol; every import block and new-file header is shown in full; the only deliberate per-step lookup ("read the body via Serena") is exact and deterministic.
- **Type/name consistency:** `log = logging.getLogger("newsbrief")` defined once in `common.py` and imported everywhere; `mode_paper` imported into `brief` so the `paper` CLI dispatch keeps working; `test_paper.py` patches `trading.requests` (matching where `fetch_stooq_price` now lives). T212 auth (`t212_auth_header`, `T212_BASE_URL`) placed in `common` because both `fetch_portfolio_weights` (brief) and `refresh_instruments_cache` (trading) consume it.
- **Risk:** the one behaviour-affecting change is moving `logging.basicConfig` into `common.py`; verified harmless because it runs once on first import and `conftest.py` sets `NEWSBRIEF_DATA_DIR` before any import. `SIGNALS_DIR` single-defined in `common` to avoid drift between `save_signals` (brief) and `mode_paper` (trading).
