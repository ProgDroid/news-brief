# Dated-Artifact Retention Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-safe retention sweep that, at the tail of each `mode_collect`, deletes dated artifact files older than 90 days and trims `signals-log.jsonl` to the same window, so the volume stops growing without bound.

**Architecture:** A new top-level `retention.py` module (pure functions + a fail-safe orchestrator `run_retention`, mirroring `claim_verify.py`/`brief_memory.py`). All target directories derive from `DATA_DIR`, so one monkeypatch relocates every family in tests. Wired as the last step of `mode_collect`, fail-safe-wrapped like the other post-deliver steps.

**Tech Stack:** Python 3 stdlib only (`os`, `re`, `json`, `datetime`, `pathlib`). No new dependencies. `pytest`, `ruff`.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-26-dated-artifact-retention-design.md`.
- **Fail-safe always:** the brief is already delivered when this runs; no retention step may raise out of `mode_collect`. `run_retention` never raises; the `mode_collect` call site also wraps it.
- **Window:** `NEWSBRIEF_RETENTION_DAYS` env (default **90**), resolved at call time. `days <= 0` ⇒ disabled no-op. Invalid env value ⇒ fall back to 90.
- **Date-only targeting:** only filenames containing a parseable `YYYY-MM-DD` are ever deleted. Undateable names are skipped/kept. This is what structurally protects bounded-state files (`book.json`, `brief_memory.json`, `feedback.json`, etc.).
- **Boundary rule:** delete strictly older than `cutoff = today − days`; a file dated exactly on the cutoff is kept.
- **`signals-log.jsonl`:** never deleted as a file (no date in name); trimmed by line, keep-on-doubt (unparseable/no-date lines kept), atomic rewrite only when something is dropped.
- **`weekly/week-*.md` excluded** from the sweep by design.
- **Commit style:** conventional commits; commit via the **Bash tool** (PowerShell prepends a BOM to commit subjects). End messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Environment:** run python/pytest/ruff via the **PowerShell tool** (Bash errors "stdin is not a tty" for Python). PowerShell may surface Python stderr as a red "NativeCommandError" even on success — judge by the pytest summary line.
- **Test gate (per `brief-local-run`):** `ruff check` + `ruff format --check` + `pytest` all pass; stage every reformatted file.

## File Structure

- **Create `retention.py`** — the whole feature: family specs, date/env helpers, `prune_dated_files`, `trim_signals_log`, `run_retention`.
- **Create `tests/test_retention.py`** — unit tests for every function + fail-safe paths, all on `tmp_path` with `DATA_DIR` monkeypatched.
- **Modify `brief.py`** — import `run_retention`; call it at the tail of `mode_collect`.
- **Modify `Dockerfile`** — add `retention.py` to the `COPY` allowlist.
- **Modify `.github/workflows/docker-publish.yml`** — add `retention.py` to the path trigger and both ruff file lists.

---

### Task 1: Module scaffold, helpers, and dated-file deletion

**Files:**
- Create: `retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `common.DATA_DIR`, `common.log`.
- Produces:
  - `DEFAULT_RETENTION_DAYS = 90`, `RETENTION_DAYS_ENV = "NEWSBRIEF_RETENTION_DAYS"`
  - `_file_date(name: str) -> datetime.date | None`
  - `_resolve_days(days) -> int`
  - `_cutoff(today: str, days: int) -> datetime.date`
  - `_families() -> list[tuple[Path, str]]`
  - `prune_dated_files(today: str, days: int) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_retention.py
import json

import retention as rt


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


def test_file_date_extracts():
    assert rt._file_date("brief-2026-06-26.md").isoformat() == "2026-06-26"
    assert rt._file_date("source_index-2026-01-02.json").isoformat() == "2026-01-02"


def test_file_date_undateable_returns_none():
    assert rt._file_date("signals-log.jsonl") is None
    assert rt._file_date("book.json") is None
    assert rt._file_date("brief-2026-13-99.md") is None  # invalid calendar date


def test_resolve_days_default(monkeypatch):
    monkeypatch.delenv("NEWSBRIEF_RETENTION_DAYS", raising=False)
    assert rt._resolve_days(None) == 90


def test_resolve_days_env_override(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "30")
    assert rt._resolve_days(None) == 30


def test_resolve_days_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "abc")
    assert rt._resolve_days(None) == 90


def test_resolve_days_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "30")
    assert rt._resolve_days(5) == 5


def test_prune_deletes_old_keeps_recent_undateable_and_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    briefs = tmp_path / "briefs"
    _touch(briefs / "brief-2026-01-01.md")  # old -> delete
    _touch(briefs / "brief-2026-06-25.md")  # recent -> keep
    _touch(tmp_path / "source_index-2026-01-01.json")  # old -> delete
    _touch(tmp_path / "claim_evidence-2026-03-28.json")  # exactly cutoff -> keep
    _touch(tmp_path / "verification-2026-06-20.json")  # recent -> keep
    _touch(tmp_path / "enrichment" / "enrichment-2026-01-01.json")  # old -> delete
    _touch(tmp_path / "signals" / "signals-2026-06-25.json")  # recent -> keep
    _touch(tmp_path / "book.json")  # undateable -> keep
    _touch(tmp_path / "signals" / "signals-log.jsonl")  # undateable -> keep

    deleted = rt.prune_dated_files("2026-06-26", 90)  # cutoff = 2026-03-28

    assert deleted == 3
    assert not (briefs / "brief-2026-01-01.md").exists()
    assert (briefs / "brief-2026-06-25.md").exists()
    assert not (tmp_path / "source_index-2026-01-01.json").exists()
    assert (tmp_path / "claim_evidence-2026-03-28.json").exists()  # boundary kept
    assert (tmp_path / "verification-2026-06-20.json").exists()
    assert not (tmp_path / "enrichment" / "enrichment-2026-01-01.json").exists()
    assert (tmp_path / "signals" / "signals-2026-06-25.json").exists()
    assert (tmp_path / "book.json").exists()
    assert (tmp_path / "signals" / "signals-log.jsonl").exists()  # not matched by signals-*.json


def test_prune_missing_dirs_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)  # no subdirs exist
    assert rt.prune_dated_files("2026-06-26", 90) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell): `python -m pytest tests/test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'retention'`.

- [ ] **Step 3: Write the module scaffold + helpers + prune**

```python
# retention.py
"""Dated-artifact retention: deletes per-day artifact files older than the
retention window and trims the rolling signals log to the same window, so the
volume does not grow without bound. Hygiene only — fail-safe (any error leaves
files in place and never affects the brief, which is already delivered). Runs at
the tail of mode_collect. Window via NEWSBRIEF_RETENTION_DAYS (default 90);
days <= 0 disables. Targets ONLY date-bearing filenames, so bounded-state files
(book.json, brief_memory.json, feedback.json, ...) are structurally untouched."""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from common import DATA_DIR, log

DEFAULT_RETENTION_DAYS = 90
RETENTION_DAYS_ENV = "NEWSBRIEF_RETENTION_DAYS"

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _families() -> list[tuple[Path, str]]:
    """(directory, glob) specs for dated-file deletion. All derived from DATA_DIR
    so a single monkeypatch in tests relocates every family. weekly/ excluded by
    design; signals-log.jsonl is not matched by 'signals-*.json' (ends in .jsonl)."""
    return [
        (DATA_DIR / "briefs", "brief-*.md"),
        (DATA_DIR, "source_index-*.json"),
        (DATA_DIR, "claim_evidence-*.json"),
        (DATA_DIR, "verification-*.json"),
        (DATA_DIR / "enrichment", "enrichment-*.json"),
        (DATA_DIR / "signals", "signals-*.json"),
    ]


def _file_date(name: str):
    """The YYYY-MM-DD embedded in a filename as a date, or None if absent/invalid.
    None means 'undateable' -> never pruned."""
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve_days(days):
    if days is not None:
        return days
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            f"Invalid {RETENTION_DAYS_ENV}={raw!r}; using {DEFAULT_RETENTION_DAYS}"
        )
        return DEFAULT_RETENTION_DAYS


def _cutoff(today: str, days: int):
    return datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=days)


def prune_dated_files(today: str, days: int) -> int:
    """Delete dated artifact files strictly older than (today - days). Files with
    no parseable date in the name are skipped. Returns the number deleted."""
    cutoff = _cutoff(today, days)
    deleted = 0
    for directory, pattern in _families():
        try:
            for path in directory.glob(pattern):
                d = _file_date(path.name)
                if d is not None and d < cutoff:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError as e:
                        log.warning(f"Retention: could not delete {path}: {e}")
        except Exception as e:
            log.warning(f"Retention: family {directory}/{pattern} skipped: {e}")
    return deleted
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `python -m pytest tests/test_retention.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add retention.py tests/test_retention.py
git commit -F - <<'EOF'
feat(retention): module scaffold + dated-file deletion

retention.py with date/env helpers and prune_dated_files: deletes dated
artifact files strictly older than today-days across 6 families, skips
undateable names (book.json etc. structurally safe). Env override
NEWSBRIEF_RETENTION_DAYS (default 90).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `signals-log.jsonl` line-trim

**Files:**
- Modify: `retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `_cutoff`, `_file_date`, `DATA_DIR`.
- Produces: `trim_signals_log(today: str, days: int) -> int` (lines dropped).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_retention.py
def test_trim_drops_old_keeps_recent_malformed_and_nodate(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    log_path = sig / "signals-log.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"ticker": "OLD", "date": "2026-01-01"}),  # old -> drop
                json.dumps({"ticker": "NEW", "date": "2026-06-25"}),  # recent -> keep
                json.dumps({"ticker": "NODATE"}),  # no date -> keep
                "{not valid json",  # malformed -> keep
            ]
        )
        + "\n"
    )
    dropped = rt.trim_signals_log("2026-06-26", 90)
    assert dropped == 1
    remaining = log_path.read_text()
    assert "OLD" not in remaining
    assert "NEW" in remaining
    assert "NODATE" in remaining
    assert "not valid json" in remaining
    # file is still valid: every non-empty line that is JSON parses
    for line in remaining.splitlines():
        if line.strip() and not line.startswith("{not"):
            json.loads(line)


def test_trim_absent_file_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    assert rt.trim_signals_log("2026-06-26", 90) == 0


def test_trim_nothing_old_leaves_file_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    p = sig / "signals-log.jsonl"
    p.write_text(json.dumps({"date": "2026-06-25"}) + "\n")
    assert rt.trim_signals_log("2026-06-26", 90) == 0
    assert "2026-06-25" in p.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell): `python -m pytest tests/test_retention.py -k trim -v`
Expected: FAIL — `AttributeError: module 'retention' has no attribute 'trim_signals_log'`.

- [ ] **Step 3: Implement the line-trim**

```python
# add to retention.py (after prune_dated_files)
def trim_signals_log(today: str, days: int) -> int:
    """Trim signals-log.jsonl to lines whose 'date' is within the window. Lines
    with no parseable date are KEPT (keep-on-doubt). Atomic rewrite only when at
    least one line is dropped. Returns the number of lines dropped."""
    path = DATA_DIR / "signals" / "signals-log.jsonl"
    if not path.exists():
        return 0
    cutoff = _cutoff(today, days)
    kept: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        keep = True
        try:
            rec = json.loads(line)
            d = _file_date(str(rec.get("date", "")))
            if d is not None and d < cutoff:
                keep = False
        except Exception:
            keep = True  # unparseable line -> keep
        if keep:
            kept.append(line)
        else:
            dropped += 1
    if dropped:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    return dropped
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `python -m pytest tests/test_retention.py -k trim -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add retention.py tests/test_retention.py
git commit -F - <<'EOF'
feat(retention): date-based line-trim for signals-log.jsonl

Drops log lines whose date is older than today-days; keeps unparseable /
no-date lines (keep-on-doubt); atomic rewrite only when something drops.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Fail-safe orchestrator `run_retention`

**Files:**
- Modify: `retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `_resolve_days`, `prune_dated_files`, `trim_signals_log`.
- Produces: `run_retention(today: str, *, days=None) -> dict` with keys `deleted`, `trimmed_lines`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_retention.py
def test_run_retention_disabled_when_days_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    _touch(tmp_path / "source_index-2020-01-01.json")
    out = rt.run_retention("2026-06-26", days=0)
    assert out == {"deleted": 0, "trimmed_lines": 0}
    assert (tmp_path / "source_index-2020-01-01.json").exists()  # nothing deleted


def test_run_retention_happy_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    _touch(tmp_path / "source_index-2020-01-01.json")  # very old -> delete
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    (sig / "signals-log.jsonl").write_text(json.dumps({"date": "2020-01-01"}) + "\n")
    out = rt.run_retention("2026-06-26", days=90)
    assert out["deleted"] == 1
    assert out["trimmed_lines"] == 1


def test_run_retention_fail_safe_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rt, "prune_dated_files", boom)
    out = rt.run_retention("2026-06-26", days=90)  # must not raise
    assert out == {"deleted": 0, "trimmed_lines": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell): `python -m pytest tests/test_retention.py -k run_retention -v`
Expected: FAIL — `AttributeError: module 'retention' has no attribute 'run_retention'`.

- [ ] **Step 3: Implement the orchestrator**

```python
# add to retention.py (after trim_signals_log)
def run_retention(today: str, *, days=None) -> dict:
    """Fail-safe entry for mode_collect. Deletes old dated files and trims the
    signals log. days<=0 disables. Never raises; returns a summary dict."""
    summary = {"deleted": 0, "trimmed_lines": 0}
    try:
        resolved = _resolve_days(days)
        if resolved <= 0:
            return summary
        summary["deleted"] = prune_dated_files(today, resolved)
        summary["trimmed_lines"] = trim_signals_log(today, resolved)
    except Exception as e:
        log.warning(f"Retention sweep skipped (brief unaffected): {e}")
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run (PowerShell): `python -m pytest tests/test_retention.py -v`
Expected: PASS (14 tests total).

- [ ] **Step 5: Commit**

```bash
git add retention.py tests/test_retention.py
git commit -F - <<'EOF'
feat(retention): fail-safe run_retention orchestrator

Resolves the window (days<=0 disables), runs prune + trim, returns
{deleted, trimmed_lines}; one outer try/except so it never raises.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Wire into `mode_collect` + Docker/CI allowlist

**Files:**
- Modify: `brief.py` (import near the other top-level imports; call at the tail of `mode_collect`'s `if raw:` block, after the trading stage)
- Modify: `Dockerfile` (the `COPY ... .py .` line)
- Modify: `.github/workflows/docker-publish.yml` (path trigger list; both ruff lines)

**Interfaces:**
- Consumes: `retention.run_retention`.

- [ ] **Step 1: Add the import to `brief.py`**

Add after the `from claim_verify import (...)` block (near line 100):
```python
from retention import run_retention
```

- [ ] **Step 2: Call the sweep at the tail of `mode_collect`**

Find the trading-stage block at the end of `mode_collect`'s `if raw:` body — it currently looks like:
```python
        try:
            mode_paper()
            book = load_book()
            msg = daily_trade_message(book, today)
            if msg:
                telegram_send(msg)
        except Exception as e:
            log.error(f"Trading stage failed (brief already delivered): {e}")
            telegram_alert(f"trading stage failed after brief: {e}")
```
Immediately AFTER that `try/except` (still inside `if raw:`, same indentation as the `try`), add:
```python
        try:
            summary = run_retention(today)
            log.info(
                f"Retention: deleted {summary['deleted']} files, "
                f"trimmed {summary['trimmed_lines']} log lines"
            )
        except Exception as e:
            log.error(f"Retention skipped (brief unaffected): {e}")
```
(Match the exact wording/indentation of the existing trading block by reading the surrounding code; other unpushed work may have shifted line numbers.)

- [ ] **Step 3: Update `Dockerfile` COPY allowlist**

From:
```dockerfile
COPY common.py trading.py validation.py brief.py brief_memory.py claim_verify.py .
```
To:
```dockerfile
COPY common.py trading.py validation.py brief.py brief_memory.py claim_verify.py retention.py .
```

- [ ] **Step 4: Update `.github/workflows/docker-publish.yml`**

Add to the path trigger list (after the `claim_verify.py` line):
```yaml
      - 'retention.py'
```
Add `retention.py` to BOTH ruff lines (after `claim_verify.py`):
```yaml
          ruff check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
          ruff format --check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
```

- [ ] **Step 5: Smoke test (PowerShell, no network)**

Run:
```
python -c "import brief; print('run_retention' in dir(brief)); print(brief.run_retention('2026-06-26', days=0))"
```
Expected: `True` then `{'deleted': 0, 'trimmed_lines': 0}`. Import must not error.

- [ ] **Step 6: Full gate (PowerShell)**

Run and confirm all green:
```
ruff check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
ruff format --check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
python -m pytest -q
```
Expected: ruff clean; whole suite passes (prior 501 + 14 new retention tests ≈ 515; report the exact count). If `ruff format --check` flags a file, run `ruff format <file>` and stage it.

- [ ] **Step 7: Commit**

```bash
git add brief.py Dockerfile .github/workflows/docker-publish.yml
git commit -F - <<'EOF'
feat(retention): run sweep at tail of mode_collect + allowlist

mode_collect calls run_retention(today) after the trading stage (fail-safe
wrapped, logs a one-line summary). Add retention.py to the Dockerfile COPY
allowlist and the CI path/ruff lists (dockerfile-copy-allowlist).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review Notes

- **Spec coverage:** global 90-day window + env override + `days<=0` disable (Task 1 `_resolve_days`, Task 3); dated-file deletion across the 6 families with date-only targeting + boundary rule (Task 1); `signals-log.jsonl` line-trim keep-on-doubt + atomic rewrite (Task 2); fail-safe orchestrator + summary (Task 3); `mode_collect` tail wiring + Docker/CI chore (Task 4); weekly excluded (absent from `_families`); bounded-state safety (asserted in Task 1's `book.json` test + structural via date-only targeting).
- **`signals-*.json` vs `signals-log.jsonl`:** Task 1's prune test explicitly asserts `signals-log.jsonl` survives the `signals-*.json` glob (end-anchored on `.json`), and `_file_date` returns None for it — double-protected.
- **Type consistency:** `today: str` ("YYYY-MM-DD") threaded through `_cutoff`, `prune_dated_files`, `trim_signals_log`, `run_retention`. `run_retention` returns `{"deleted", "trimmed_lines"}` consistently (defined Task 3, logged Task 4). `_file_date` returns `date | None` everywhere. Family dirs all derived from `DATA_DIR` so the single `monkeypatch.setattr(rt, "DATA_DIR", tmp_path)` relocates them all.
