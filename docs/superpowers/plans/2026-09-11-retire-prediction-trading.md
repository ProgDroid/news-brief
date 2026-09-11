# Retire Prediction-Market Trading — Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> v3, 2026-09-11: v2 plus the fix list of the second review (`...-redteam-v2.md`): two tests moved from Task 4 to Task 3, `pgdiag` added to the pruning patterns, the live-guard deletion moved to Task 4, the `_closed` fixture signature corrected, the acceptance grep replaced with a `git grep` pair and a pre-registered residual, and the runbook's precondition turned from an assertion into an operator-executed measurement that gates the push.
> v2, 2026-09-11: revised after the red-team review in `docs/superpowers/reviews/2026-09-11-retire-prediction-trading-redteam.md`. Tasks 3+4 of v1 are merged (v1's Task 3 could not be green: `tests/test_prediction.py` imports `polygram_live` at module scope, `/predict` reads `PG_LIVE_*`, and the stub failed ruff). Two operator decisions recorded: **retired paper rows ARE scored** as if closed at their last mark; **`asset_class == "prediction"` is excluded from every aggregate**.

**Goal:** Remove every line of prediction-market trading from news-brief — the polygram.ink venue client, Sleeve A (live favourite-fade), Sleeve B (`/predict` + thesis log), the paper prediction sleeve (matcher, price feed, marks, settlement), `pgdiag`, the 18 `PG_*`/prediction knobs and their compose anchors — while leaving equity/crypto paper trading, the `index` market-pulse class and the `/thesis` annotation feature untouched, and keeping every historical prediction row in the book as record.

**Architecture:** Six commits on `main`, each green under the full gate. Order: tests decoupled from the knobs they borrow as examples (no production change); a host script that closes and scores the open paper prediction rows (ships before the code that priced them disappears); then the venue client, both sleeves and pgdiag in ONE commit (they are import-coupled); then the paper sleeve and every shared branch; then README/docs; then the record, beads, memory, host runbook, push.

**Tech Stack:** Python 3.14, pytest, ruff. Run Python as `py` from the Bash tool (never `python`). Commit with `git commit -F <file>`. `sed -i` only on RELATIVE paths (the guard denies `sed` on a Windows-form path).

**Spec:** `docs/2026-09-11-live-buy-pricing.md` (the measurement); the decision record `docs/2026-09-11-prediction-trading-retired.md` (written in Task 6); the removal inventory (a session-local file; its substance is reproduced in the task lists below); the red-team review above. Baseline: **1904 tests collected**, 1680 `def test_` lines.

## Global Constraints

- Every commit passes `ruff check .`, `ruff format --check .`, `py -m pytest -q` with 0 failures. **The DB-backed tests must RUN for Tasks 1 and 3** (they touch `tests/test_config.py`, which is DB-only): start the test Postgres first and export `DATABASE_URL` —
  ```sh
  docker run --rm -d --name nb-test-pg -p 5432:5432 --tmpfs /var/lib/postgresql/data \
    -e POSTGRES_PASSWORD=newsbrief -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
  export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
  py -m pytest tests/test_config.py -q   # must report passes, not "skipped"
  ```
- `common.KNOBS` and the `docker-compose.yml` anchor change in the **same commit** (`tests/test_packaging.py::test_every_variable_the_anchor_passes_through_is_read_by_something`). The repo compose has ONE service (`newsbrief`) with the anchor; verify with `grep -c "PG_A_ENABLED" docker-compose.yml` → 1 before, 0 after.
- `thesis_ref`, `THESIS_FILE = theses.json`, `/thesis`, `load_theses/save_theses` are the **/thesis annotation feature** and STAY. Only `THESIS_LOG_FILE = thesis_log.json` + `load_thesis_log/save_thesis_log/append_thesis` (Sleeve B) go.
- The signals schema (`_EMIT_SIGNALS_TOOL`, `_ASSET_CLASSES` in brief.py, `normalize_signals`) is venue-agnostic — **do not edit it**.
- Historical rows with `asset_class == "prediction"` or `execution == "live"` remain in `book.json`. **`aggregate_performance` excludes BOTH** (decided 2026-09-11): the live returns measure the venue's pricing, and prediction is a class the model can no longer emit, so neither belongs in the weekly report, the gate, or `performance_prompt_block`. Tests that prove tolerance of these rows MUST exist and will name the class — the acceptance greps allow-list them explicitly.
- `play_type` and `entry_spread` are removed from NEW rows and `play_type` from `_DIMENSIONS`; old rows keep whatever they carry.
- Never `git add -A`; add explicit paths. Leave `.beads/` and `.claude/memory/` out of these commits.
- Pre-register counts per task and compare. The final `def test_` count is re-derived in Task 6 from the tree, not from arithmetic in this document.

---

### Task 1: Decouple the config and packaging tests from the PG example knobs

**Files:**
- Modify: `tests/test_config.py` (18 tests; 50 `PG_` occurrences), `tests/test_packaging.py:247-248` + docstring `256-260`

**Interfaces:**
- Consumes: surviving knobs `BRIEF_MEMORY_ENABLED` (bool, False, in the anchor at compose:122), `CLAIM_VERIFY_ENABLED` (bool, False, anchor :123), `GATE_MIN_HIT_RATE` (float, 0.55), `VOL_SPIKE_MULT` (float, 2.5).
- Produces: no test names a `PG_*` knob.

- [ ] **Step 1: Pre-register.** With `DATABASE_URL` exported: `py -m pytest tests/test_config.py tests/test_packaging.py -q` → record `N passed` (must NOT say 62 skipped). `grep -c "PG_" tests/test_config.py tests/test_packaging.py` → 50, 4.
- [ ] **Step 2: Substitute names only.** `sed -i 's/PG_A_ENABLED/BRIEF_MEMORY_ENABLED/g; s/PG_B_ENABLED/CLAIM_VERIFY_ENABLED/g; s/PG_A_STAKE/GATE_MIN_HIT_RATE/g' tests/test_config.py tests/test_packaging.py`. Then open every hunk that compares a knob to a NUMERIC literal and ask what the literal is: **a value the test itself set** (`"9.0"`, `"7.5"`, an env string) stays; **the knob's built-in default** (`2.0` for the old stake) becomes `0.55`. `test_config.py:250` and `:750` assert an imported env value — leave their literals alone. Check `test_an_unparseable_boolean_is_reported_though_it_coerces_to_false` (~773) now uses two DIFFERENT bool knobs (it should, after the sed).
- [ ] **Step 3: Verify.** `grep -c "PG_" tests/test_config.py tests/test_packaging.py` → 0, 0. `py -m pytest tests/test_config.py tests/test_packaging.py -q` → same `N passed` as Step 1, 0 failed, 0 skipped-for-DB.
- [ ] **Step 4: Gate + commit.** Full gate (with `DATABASE_URL`). `git add tests/test_config.py tests/test_packaging.py` — "test(config): borrow surviving knobs as examples, not PG_*".

---

### Task 2: Host script that retires and scores the open paper prediction rows

**Files:**
- Create: `scripts/retire_prediction_rows.py`
- Test: `tests/test_retire_prediction_rows.py`

**Interfaces:**
- Produces: `plan(book, *, today) -> list[tuple[dict, dict]]` (pure; raises `Refused`), `main(argv) -> int` (0 ok, 2 refused), `Refused`, `score(row) -> dict`.
- Row contract for an open paper prediction row: `status="closed"`, `close_reason="venue_retired"`, `closed_date=today`. If `last_mark` exists: `realized_return = last_mark["return"]` and the row is **scored exactly as the retired `_stamp_close_metrics` prediction branch scored a close** — `haircut = entry_spread if not None else 0.02` (200 bps, the `HAIRCUT_BPS_PREDICTION` default), `+0.02` more when `play_type == "momentum"`; `net_return = max(realized_return − haircut, −1.0)`; `benchmark_return = 0.0`; `edge = net_return`. If no `last_mark`: `realized_return`, `haircut`, `net_return`, `benchmark_return`, `edge` all `None` — unknown, not zero. Live rows are never touched; any OPEN live row is a refusal.

- [ ] **Step 1: Write the failing tests.**

```python
"""Tests for the one-off retirement of open paper prediction rows (news-brief-oh4).

polygram.ink was dropped as a venue on 2026-09-11. The paper prediction sleeve
took its prices from the same API, so its open rows can never be marked again.
Decided by the operator: they close at their last known mark, reason
venue_retired, are SCORED as if that mark were the close (mirroring the retired
_stamp_close_metrics prediction branch), and are KEPT.
"""

import json

import pytest

from scripts import retire_prediction_rows as retire


def _pred(mid, *, status="open", last_mark=None, execution="paper", **over):
    row = {
        "id": f"2026-08-20:prediction:{mid}:YES:{execution}",
        "asset_class": "prediction",
        "execution": execution,
        "status": status,
        "instrument": mid,
        "ticker": mid,
        "direction": "bullish",
        "play_type": "resolution",
        "entry_price": 0.4,
        "entry_spread": 0.015,
        "last_mark": last_mark,
        "close_reason": None,
        "closed_date": None,
        "realized_return": None,
    }
    row.update(over)
    return row


def _book():
    return {
        "positions": [
            _pred("m1", last_mark={"date": "2026-09-05", "price": 0.5, "return": 0.25}),
            _pred("m2"),  # never marked
            _pred("m3", status="closed", close_reason="settlement", realized_return=1.0),
            _pred("m4", execution="live", status="closed", close_reason="settled"),
            _pred(
                "m5",
                play_type="momentum",
                entry_spread=None,
                last_mark={"date": "2026-09-05", "price": 0.1, "return": -0.99},
            ),
            {"id": "eq", "asset_class": "equity", "status": "open", "ticker": "SHEL"},
        ]
    }


def _plan(book, today="2026-09-12"):
    return {row["instrument"]: ch for row, ch in retire.plan(book, today=today)}


def test_open_paper_prediction_rows_close_and_score_at_their_last_mark():
    ch = _plan(_book())["m1"]
    assert ch["status"] == "closed" and ch["close_reason"] == "venue_retired"
    assert ch["closed_date"] == "2026-09-12"
    assert ch["realized_return"] == 0.25
    assert ch["haircut"] == 0.015  # the orderbook half-spread captured at open
    assert abs(ch["net_return"] - 0.235) < 1e-12
    assert ch["benchmark_return"] == 0.0 and abs(ch["edge"] - 0.235) < 1e-12


def test_scoring_mirrors_the_retired_branch_for_momentum_and_the_floor():
    """momentum pays a second 200bps leg; no entry_spread falls back to the 200bps
    default; and a stake cannot lose more than itself."""
    ch = _plan(_book())["m5"]
    assert abs(ch["haircut"] - 0.04) < 1e-12  # 0.02 default + 0.02 momentum exit leg
    assert ch["net_return"] == -1.0  # -0.99 - 0.04 floored


def test_a_row_never_marked_retires_unscored():
    ch = _plan(_book())["m2"]
    assert ch["status"] == "closed" and ch["close_reason"] == "venue_retired"
    for k in ("realized_return", "haircut", "net_return", "benchmark_return", "edge"):
        assert ch[k] is None, k


def test_closed_rows_live_rows_and_other_classes_are_untouched():
    touched = set(_plan(_book()))
    assert touched == {"m1", "m2", "m5"}


def test_refuses_while_a_live_row_is_still_open():
    book = _book()
    book["positions"].append(_pred("m9", execution="live"))
    with pytest.raises(retire.Refused, match="live"):
        retire.plan(book, today="2026-09-12")


def test_nothing_to_do_is_an_empty_plan_not_a_refusal():
    book = {"positions": [{"id": "eq", "asset_class": "equity", "status": "open"}]}
    assert retire.plan(book, today="2026-09-12") == []


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert retire.main([str(path)]) == 0
    assert path.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "3 row(s) to retire, 1 never marked" in out


def test_apply_writes_and_a_second_run_finds_nothing(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    assert retire.main([str(path), "--apply"]) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    m1 = next(p for p in after["positions"] if p.get("instrument") == "m1")
    assert m1["status"] == "closed" and abs(m1["net_return"] - 0.235) < 1e-12
    assert list(tmp_path.glob("book.*.bak"))
    assert retire.plan(after, today="2026-09-12") == []
```

- [ ] **Step 2: RED.** `py -m pytest tests/test_retire_prediction_rows.py -q` → `ImportError: cannot import name 'retire_prediction_rows'`.
- [ ] **Step 3: Write the script.**

```python
"""One-off retirement of the open paper prediction rows.

Context: news-brief-oh4. polygram.ink was dropped as a venue on 2026-09-11 (its
buy path charged ~$1 per share whatever the displayed price; see
docs/2026-09-11-live-buy-pricing.md and docs/2026-09-11-prediction-trading-retired.md).
The paper prediction sleeve took its market prices from the same API, so its open
rows can never be marked again.

Decided by the operator, 2026-09-11: each open row closes at the last mark the
weekly run recorded (reason `venue_retired`), is SCORED as if that mark were the
close, and is KEPT -- every prediction row is history. The scoring reproduces the
prediction branch of trading._stamp_close_metrics as it stood when it was removed,
because that code no longer exists to call:

    haircut = entry_spread (orderbook half-spread captured at open) else 0.02
              (HAIRCUT_BPS_PREDICTION's default, 200 bps); +0.02 for a momentum play
    net_return = max(realized_return - haircut, -1.0)   # a stake cannot lose more than itself
    benchmark_return = 0.0                               # the naive coin-flip baseline
    edge = net_return - 0.0

A row that was never marked closes with every score None: unknown, not zero.

Refuses if any LIVE row is still open -- that is real capital and this script
must never be the thing that "closes" it. Run after deploying the commit that
removed the prediction code (that code's weekly mark path now leaves these rows
alone and logs how many are waiting).

Usage (dry run prints the change and writes nothing):

    docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json
    docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

CLOSE_REASON = "venue_retired"
DEFAULT_HAIRCUT = 0.02  # HAIRCUT_BPS_PREDICTION default (200 bps) at retirement
NAIVE_BENCHMARK = 0.0


class Refused(Exception):
    """The book does not look the way this repair was reasoned about."""


def score(row: dict) -> dict:
    """The retired _stamp_close_metrics prediction branch, applied to the last mark."""
    mark = row.get("last_mark") or {}
    gross = mark.get("return")
    if gross is None:
        return {k: None for k in ("realized_return", "haircut", "net_return", "benchmark_return", "edge")}
    haircut = row.get("entry_spread")
    if haircut is None:
        haircut = DEFAULT_HAIRCUT
    if row.get("play_type") == "momentum":
        haircut += DEFAULT_HAIRCUT
    net = max(gross - haircut, -1.0)
    return {
        "realized_return": gross,
        "haircut": haircut,
        "net_return": net,
        "benchmark_return": NAIVE_BENCHMARK,
        "edge": net - NAIVE_BENCHMARK,
    }


def plan(book: dict, *, today: str) -> list[tuple[dict, dict]]:
    """(row, changes) for every open paper prediction row. Pure. Raises Refused."""
    positions = book.get("positions", [])
    live_open = [
        p for p in positions if p.get("execution") == "live" and p.get("status") == "open"
    ]
    if live_open:
        raise Refused(
            f"{len(live_open)} live row(s) still open ({[p.get('id') for p in live_open]}); "
            "real capital is not retired by this script"
        )
    out = []
    for p in positions:
        if p.get("asset_class") != "prediction" or p.get("status") != "open":
            continue
        changes = {"status": "closed", "close_reason": CLOSE_REASON, "closed_date": today}
        changes.update(score(p))
        out.append((p, changes))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book", type=Path, help="path to book.json")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args(argv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    book = json.loads(args.book.read_text(encoding="utf-8"))
    try:
        changes = plan(book, today=today)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    for row, ch in changes:
        print(f"row {row['id']}")
        for k, v in ch.items():
            print(f"  {k:<16} {row.get(k)!r} -> {v!r}")
    unmarked = sum(1 for _, ch in changes if ch["net_return"] is None)
    print(f"\n{len(changes)} row(s) to retire, {unmarked} never marked")

    if not args.apply:
        print("\ndry run; nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.book.with_suffix(f".{stamp}.bak")
    shutil.copy2(args.book, backup)
    for row, ch in changes:
        row.update(ch)
    args.book.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"\nwritten. backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: GREEN.** `py -m pytest tests/test_retire_prediction_rows.py tests/test_packaging.py -q` → 8 new passing; packaging's `scripts/*.py` import parametrization picks it up.
- [ ] **Step 5: Gate + commit.** `git add scripts/retire_prediction_rows.py tests/test_retire_prediction_rows.py` — "feat(scripts): retire and score the open paper prediction rows at their last mark".

---

### Task 3: Remove the venue client, both sleeves, pgdiag and the live-book repairs (one commit — they are import-coupled)

**Files:**
- Delete: `polygram_live.py`, `tests/test_polygram_live.py`, `scripts/repair_live_cost_basis.py`, `scripts/repair_lost_live_close.py`, `scripts/repair_settled_payouts.py`, `tests/test_repair_live_cost_basis.py`, `tests/test_repair_lost_live_close.py`, `tests/test_repair_settled_payouts.py`
- Modify: `trading.py`, `brief.py`, `validation.py`, `common.py`, `retention.py`, `supervisor.py:558`, `docker-compose.yml`, `Dockerfile:39`, `.github/workflows/docker-publish.yml:17,75,76`, `tests/test_prediction.py`, `tests/test_trading.py`, `tests/test_monitor.py`, `tests/test_validation.py`, `tests/test_commands.py`, `tests/test_common.py`, `tests/test_retention.py`

**Interfaces:**
- Produces: `trading.mode_paper()` still returns `{"opened": int, "sleeve_a": None}` (shape changes in Task 4); `validation.daily_trade_message(book, today, sleeve_a=None)` keeps its third parameter, ignored (dropped in Task 4); `retention.run_retention` returns `{"deleted": int, "trimmed_lines": int}`; `common` loses `THESIS_LOG_FILE`, `load_thesis_log`, `save_thesis_log`, `append_thesis` and the 16 `PG_*` knobs (the two `*_PREDICTION` knobs go in Task 4).

- [ ] **Step 1: Pre-register.** Whole files: 54 + 11 + 11 + 8 = 84. `tests/test_prediction.py`: remove `import polygram_live` (line 9) and every test that references `polygram_live`, `open_sleeve_a_live`, `sweep_live_exits`, `_sleeve_a_exit_reason`, `_sleeve_a_entry_reason`, `_fetch_pg_half_spread`, `score_settled_theses`, `_sleeve_b_open_ok`, `_predict_`, `PG_LIVE`, `PG_A_`, `PG_B_`, `thesis_log`, `pgdiag`/`mode_pgdiag`, or `_mtm_prediction`-with-a-live-row (`test_mark_to_market_skips_live_rows`) — the second review counted **47** of 79 with `pgdiag` included; count them yourself with `grep -c` per pattern and by reading, and write the number down. Named: `test_trading.py` 3 (`test_fetch_pg_half_spread_parses_levels`, `test_fetch_pg_half_spread_none_on_garbage`, `test_stamp_open_benchmark_prediction` — it monkeypatches `_fetch_pg_half_spread`, which this task deletes); `test_monitor.py` 1 (`test_mode_monitor_runs_live_exit_and_reconcile`); `test_validation.py` 8 (`test_live_performance_reports_live_only`, `test_daily_trade_message_separates_live_from_paper`, `test_sleeve_a_block_reports_flags_when_off`, `test_sleeve_a_block_flags_faults_but_not_design_declines`, `test_sleeve_a_block_marks_unreadable_wallet`, `test_sleeve_a_block_reports_a_crash`, `test_a_live_row_shows_the_price_paid_and_names_a_fill_above_the_band`, `test_a_live_row_inside_the_band_carries_no_warning`; **KEEP** `test_aggregate_performance_excludes_live` and `test_daily_trade_message_still_empty_without_status_or_positions` — the second becomes a two-arg call in Task 4); `test_commands.py` 10 (`test_close_picker_labels_prediction_by_question` — asserts the 💵 label this task deletes, `test_close_ticker_routes_live_to_venue_sell`, `test_close_reports_a_live_row_it_could_NOT_sell`, `test_predict_wizard_thesis_to_market`, `test_predict_disabled_says_so`, `test_predict_commit_opens_and_logs`, `test_predict_commit_blocked_by_cap`, `test_predict_commit_passes_real_live_exposure`, `test_predict_stake_respects_effective_cap`, `test_predict_commit_reports_the_price_PAID_not_the_displayed_one`); `test_common.py` 2 (`test_append_thesis_persists`, `test_load_thesis_log_missing_is_empty`); `test_retention.py` 1 (`test_prune_scored_theses`) + 2 edits (summary dict at :135, :158). Total named = **25** (3+1+8+10+2+1) + **47** from test_prediction = 72, plus 84 whole-file = **156** `def test_` lines removed.

- [ ] **Step 2: Behavioural test edit first.** `tests/test_retention.py:135` and `:158` → `assert out == {"deleted": 0, "trimmed_lines": 0}`. Run → 2 FAIL (extra key). Then `retention.py`: delete `prune_scored_theses`, `_SUMMARY_KEYS` (sole consumer), `load_thesis_log, save_thesis_log` from the import, and any now-unused `datetime`/`timedelta` import (ruff says); `run_retention`:
  ```python
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
  Re-run → GREEN; delete `test_prune_scored_theses`.

- [ ] **Step 3: Delete files.** `git rm polygram_live.py tests/test_polygram_live.py scripts/repair_live_cost_basis.py scripts/repair_lost_live_close.py scripts/repair_settled_payouts.py tests/test_repair_live_cost_basis.py tests/test_repair_lost_live_close.py tests/test_repair_settled_payouts.py`.

- [ ] **Step 4: trading.py.** Delete `_fetch_pg_half_spread`, `_sleeve_a_entry_reason`, `_sleeve_b_open_ok`, `score_settled_theses`, `_SLEEVE_A_BLOCKED_CAP`, `open_sleeve_a_live`, `_sleeve_a_exit_reason`, `sweep_live_exits`. In `mode_paper` replace the whole `sleeve_a = None ... try/except` block with `sleeve_a = None` (keep the dict return for now). In `_stamp_open_benchmark` the prediction block becomes `p["benchmark_entry"] = None; p["entry_spread"] = None; return` (whole function rewritten in Task 4). Leave `mark_to_market`'s `execution == "live"` guard in place for this commit — with `_mtm_prediction` still present, an open live row would otherwise flow into it; the guard goes in Task 4 together with the dispatch it protects.

- [ ] **Step 5: brief.py.** Delete `import polygram_live`; `mode_pgdiag`; `"pgdiag": mode_pgdiag,` in `MODES`; `pgdiag` in the usage string and module docstring; lines 952–981 (the `pr_thesis`/`pr_stake_text`/`pr_phat` branches of `_wizard_handle_text`); the whole `/predict` block 1007–1251; the `pr:cancel` block; the six `pr:` `elif` branches; `elif text == "/predict"`; the `("predict", ...)` tuple in `BOT_COMMANDS`; the entire `with file_lock(...)` live block in `mode_monitor` (sweep/reconcile/backfill/score) and its `try/except`; rewrite `mode_monitor`'s docstring to `"""Hourly cross-asset volume-anomaly alerts plus the capture/comprehend/knob health checks."""`. Comment at 1503 → `# ── /addsource wizard (cancel works without a live wizard) ──`. `_close_ticker`:
  ```python
          closed_n = 0
          failed = []
          for p in matches:
              if _close_position_at_market(p, day, "manual"):
                  closed_n += 1
              else:
                  failed.append(p)
  ```
  delete the `# Live rows hold real capital...` comment and the 8fy comment block; the partial-failure message ends `". Check the logs."`. `_close_picker_render`: delete the two `if p.get("execution") == "live": label = f"💵 {label}"` lines. After the edits run `ruff check brief.py` — `file_lock`, `load_book`, `save_book` are still used by `_close_ticker`; if ruff reports any import unused, delete it.

- [ ] **Step 6: validation.py.** Delete `live_performance`, `_SKIP_LABELS`, `_SLEEVE_A_FAULTS`, `_sleeve_a_block`. `_pred_lines(p)` drops the `live` keyword: `tail = ["paper"]`, then the `play_type` line, then the handle (function deleted in Task 4). `daily_trade_message`: drop `opened_live`, `sleeve_block`, the LIVE section, `lines.extend(sleeve_block)`; condition `if not (opened or open_now): return ""`; open-positions tag is the literal `"paper"`. `performance_report`: delete the `lp = live_performance(book)` tail. `aggregate_performance`'s filter comment: `# execution == "live" rows are the retired polygram book: their returns measure the venue's pricing, not the strategy (docs/2026-09-11-live-buy-pricing.md).`

- [ ] **Step 7: common.py, compose, packaging, docstrings.** Delete the 16 knobs `PG_LIVE_ENABLED` … `PG_THESIS_GRACE_DAYS` (common.py 188–206) and their 16 compose lines (83–98); delete the compose `pgdiag` comment (28–32); rewrite the compose header at 69–72 so the SEED VALUES paragraph introduces the knob lines generically (`# Runtime knobs. All default to off/empty, and an empty value keeps common.py's built-in default — so omitting them from your .env is exactly as safe as not having these lines at all.`). Delete `THESIS_LOG_FILE`, `load_thesis_log`, `save_thesis_log`, `append_thesis` and their section header. Example swaps: common.py:134 `common.PG_A_ENABLED` → `common.BRIEF_MEMORY_ENABLED`; :313 `PG_A_STAKE=banana coerces to 2.0` → `VOL_SPIKE_MULT=banana coerces to 2.5`; :365 `"PG_A_ENABLED"` → `"BRIEF_MEMORY_ENABLED"`; `tests/test_common.py:153-157` the deliberate typo `PG_A_ENABLD` → `BRIEF_MEMORY_ENABLD`; `tests/test_common.py:188` and `tests/test_job_interlock.py:352` docstrings: replace the knob/function name with a surviving one. `Dockerfile:39` and workflow :17/:75/:76 drop `polygram_live.py`. `supervisor.py:558` → `which sends Telegram alerts — a duplicate run is a duplicate message to the operator.`

- [ ] **Step 8: Verify.** `grep -rn "polygram_live\|sweep_live_exits\|open_sleeve_a_live\|_sleeve_a_\|_sleeve_b_\|live_performance\|pgdiag\|PG_LIVE\|PG_A_\|PG_B_\|PG_THESIS\|thesis_log\|append_thesis\|_predict_\|\"pr:\|score_settled" --include=*.py --include=*.yml --include=Dockerfile . | grep -v "^./docs\|^./from-server"` → nothing. Full gate with `DATABASE_URL`; 0 failed; `def test_` delta = −(84 + 24 + test_prediction count).

- [ ] **Step 9: Commit.** "refactor(trading): remove the polygram venue client, both sleeves and pgdiag".

---

### Task 4: Remove the paper prediction sleeve and every shared prediction branch

**Files:**
- Delete: `tests/test_prediction.py` (remaining ~33 tests; relocate two)
- Modify: `trading.py`, `brief.py`, `validation.py`, `common.py`, `docker-compose.yml` (POLYGRAM lines), `enrichment/config.py:10`, `comprehend.py:690`, `tests/test_trading.py`, `tests/test_monitor.py`, `tests/test_validation.py`, `tests/test_commands.py`, `tests/test_checkpoint_backfill.py`, `tests/test_enrichment_config.py`

**Interfaces:**
- Produces: `trading.mode_paper() -> {"opened": int}`; `validation.daily_trade_message(book, today) -> str`; `validation._DIMENSIONS = ("asset_class", "confidence", "thesis_ref", "source_kind", "source_perspective")`; `validation._ASSET_CLASSES = ("equity", "crypto")`; `validation.aggregate_performance` excludes `asset_class == "prediction"` and `execution == "live"`; `trading._VENUE_BY_ASSET = {"equity": "t212", "crypto": "kraken"}`; `common` loses `POLYGRAM_EMAIL`, `POLYGRAM_PASSWORD`, `HAIRCUT_BPS_PREDICTION`, `VOL_FLOOR_PREDICTION`; new rows carry neither `play_type` nor `entry_spread`.

- [ ] **Step 1: Pre-register.** Remaining `test_prediction.py` tests (count them). Named deletions: `test_trading.py` 3 (`test_stamp_close_metrics_prediction_resolution`, `test_stamp_close_metrics_prediction_momentum_fallback`, `test_prediction_total_loss_cannot_exceed_minus_100pct`); `test_monitor.py` 5 (`test_pg_volume_reads_market_field`, `test_pg_volume_missing_field_returns_none`, `test_pg_volume_unfetchable_market_returns_none`, `test_resolve_watch_prediction_validates_market`, `test_resolve_watch_prediction_bad_market_returns_none`); `test_validation.py` 3 (`test_daily_trade_message_names_prediction_markets`, `..._truncates_long_questions`, `..._falls_back_to_id_without_question`) + delete the `_pred_row` helper; `test_commands.py` 1 (`test_watch_explicit_prediction`); `test_checkpoint_backfill.py` 1 (`test_historical_closes_prediction_returns_empty`). Additions: **+5** — `test_collect_trading_failure_does_not_duplicate_brief` relocated, and four new tests below (the first of which is the rewritten form of `test_prediction.py::test_mode_paper_returns_summary_when_nothing_to_do`, so it is not counted twice). Edits: `test_trading.py:48` (`play_type is None`), `:71` (`entry_spread is None` — delete the assertion), `test_monitor.py::test_vol_config_defaults_present` (drop `VOL_FLOOR_PREDICTION`), `::test_fetch_volume_dispatches_by_asset_class` (drop the prediction arm), `test_validation.py::test_daily_trade_message_opened_and_open` (drop the `mkt1` row + assertion, drop `"play_type": None` keys, and from the `_closed` helper), `::test_daily_trade_message_still_empty_without_status_or_positions` (two-arg call), `::test_aggregate_performance_excludes_live` (its PAPER row becomes `asset_class: "equity"` — with both rows `prediction` the new filter empties the sample and `overall` is `None`), `test_enrichment_config.py:49-52` (`PG_A_ENABLED` → `BRIEF_MEMORY_ENABLED` in the `match=`).

- [ ] **Step 2: New and relocated tests FIRST (red where they can be).** Into `tests/test_trading.py`:
  ```python
  def test_mode_paper_returns_a_summary_when_there_is_nothing_to_do(monkeypatch, tmp_path):
      """mode_collect does `summary = mode_paper() or {}`; a bare int or None there
      is a TypeError on a quiet day."""
      monkeypatch.setattr(trading, "SIGNALS_DIR", tmp_path)
      assert trading.mode_paper() == {"opened": 0}


  def test_mark_to_market_leaves_a_retired_prediction_row_alone_and_says_so(monkeypatch, caplog):
      """No price source exists for the class any more. The row is not priced, not
      closed, and the weekly run says how many are waiting for the retire script --
      a silent skip would be unattributable (fail-closed-needs-status-not-count)."""
      caplog.set_level("WARNING", logger="newsbrief")
      monkeypatch.setattr(trading, "fetch_price", lambda *a, **k: pytest.fail("priced"))
      row = {"status": "open", "asset_class": "prediction", "instrument": "3324624",
             "ticker": "3324624", "direction": "bullish", "entry_price": 0.4,
             "entry_date": "2026-08-20", "checkpoints": {}, "last_mark": None}
      book = trading.mark_to_market({"positions": [row]}, "2026-09-12")
      assert book["positions"][0]["status"] == "open"
      assert any("1 prediction row(s) still open" in r.message for r in caplog.records)
  ```
  plus `test_collect_trading_failure_does_not_duplicate_brief` moved verbatim from `test_prediction.py:594–626` with the raised text changed to `"quote provider down"`. Into `tests/test_validation.py`:
  ```python
  def test_aggregate_performance_excludes_the_retired_prediction_class():
      """Decided 2026-09-11: prediction rows stay in the book as record and count
      nowhere -- not the weekly report, not the gate, not the daily prompt."""
      book = {"positions": [_closed("prediction", 0.5), _closed("equity", 0.1)]}
      # _closed(asset_class, net, edge=None, confidence=None, play_type=None, thesis_ref=None)
      agg = validation.aggregate_performance(book)
      assert agg["overall"]["n"] == 1
      assert "prediction" not in agg["dimensions"]["asset_class"]
  ```
  Into `tests/test_commands.py`, next to `test_close_says_plainly_when_everything_closed`:
  ```python
  def test_close_on_a_not_yet_retired_prediction_row_names_the_script(monkeypatch):
      book = {"positions": [{"id": "x", "status": "open", "asset_class": "prediction",
                             "ticker": "3324624", "instrument": "3324624",
                             "direction": "bullish", "entry_price": 0.4}]}
      monkeypatch.setattr(brief, "load_book", lambda: book)
      monkeypatch.setattr(brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext())
      sent = []
      monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t))
      brief._close_ticker("3324624")
      assert "retire_prediction_rows" in sent[-1] and book["positions"][0]["status"] == "open"
  ```
  Run them: the first FAILS on shape; the second FAILS because the warning line is absent (today `price_position` routes prediction to `polygram_market`, not `fetch_price`, so the `pytest.fail("priced")` control is inert before the change and only bites in the mutation check); the third FAILS because prediction is still aggregated (`overall.n == 2`); the fourth FAILS with `⚠️ Couldn't close 3324624 — left open.`. The `/close` test does not patch `save_book` — safe only because the retired branch returns BEFORE the close loop; keep it there. Record which passed early; a test that passes before the change proves nothing and must be mutation-checked later.

- [ ] **Step 3: trading.py deletions.** `import re` (confirm sole use with `grep -n "\bre\." trading.py`), `ANTHROPIC_HEADERS` and `POLYGRAM_EMAIL, POLYGRAM_PASSWORD` from the common import, constants block 128–137, `fetch_pg_volume`, `_parse_pg_market`, `_pg_outcome_label`, `polygram_login`, `_polygram_get`, `polygram_search`, `polygram_market`, `_pg_market_volume`, `_signal_search_terms`, `_gather_pg_candidates`, `_parse_matches`, `run_prediction_matcher`, `_pg_match_pass`, `_open_prediction_positions`, `_settle_prediction`, `_mtm_prediction`.

- [ ] **Step 4: trading.py shared edits.** Replacement bodies:

  `_VENUE_BY_ASSET = {"equity": "t212", "crypto": "kraken"}`

  ```python
  def fetch_volume(asset_class: str, instrument: str) -> float | None:
      """Mark one instrument's volume via the fetcher for its asset class."""
      if asset_class == "crypto":
          return fetch_kraken_volume(instrument)
      # index instruments are raw Yahoo symbols ("BZ=F"), which fetch_quote's
      # base.market parsing cannot read — go straight to Yahoo, as fetch_price does.
      q = _yahoo_fetch(instrument) if asset_class == "index" else fetch_quote(instrument)
      return q.volume if q else None
  ```
  ```python
  def _stamp_open_benchmark(p: dict) -> None:
      """Stamp benchmark_entry on a freshly opened position.

      Best-effort: any fetch failure leaves the field None and never raises.
      """
      try:
          p["benchmark_entry"] = fetch_benchmark_level(p.get("asset_class", "equity"))
      except Exception as e:
          log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
          p["benchmark_entry"] = None
  ```
  ```python
  def _haircut_fraction(p: dict) -> float:
      """Round-trip cost fraction for a closed position: one config constant per class."""
      if p.get("asset_class", "equity") == "crypto":
          return common.HAIRCUT_BPS_CRYPTO / 10_000
      return common.HAIRCUT_BPS_EQUITY / 10_000
  ```
  `_stamp_close_metrics`: delete the `net = max(net, -1.0)` block and its comment; replace `bench = None / if ac == "prediction": bench = 0.0 / else:` + indented block with the de-indented block:
  ```python
      p["net_return"] = net
      bench = None
      entry = p.get("benchmark_entry")
      if entry:
          try:
              level = fetch_benchmark_level(ac)
          except Exception as e:
              log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
              level = None
          if level is not None:
              candidate = _signal_return("bullish", entry, level)
              if abs(candidate) > BENCHMARK_SANITY_RETURN:
                  # Corrupt level on one side. Leaving edge unset costs one row
                  # of attribution; stamping it poisons every mean downstream,
                  # including the model's own performance_prompt_block.
                  log.warning(
                      f"Implausible benchmark return {candidate:.2%} for "
                      f"{p.get('ticker')} (entry={entry}, level={level}) — "
                      "edge left unset"
                  )
              else:
                  bench = candidate
      p["benchmark_return"] = bench
      p["edge"] = (net - bench) if bench is not None else None
  ```
  ```python
  def price_position(p: dict) -> float | None:
      """Mark a position to market via fetch_price for its asset class."""
      return fetch_price(p.get("asset_class", "equity"), p["instrument"])
  ```
  `_migrate_position`: delete `p.setdefault("play_type", None)` and `/play_type` in the docstring at ~1274. `mode_paper` row literal (~2007): delete `"play_type": None,`. `resolve_watch_entry`: delete the prediction block and the docstring's last two sentences. `_floor_for`: delete the `"prediction"` key. `mode_paper`: delete `match_pass = ...`, `opened += _open_prediction_positions(...)`, `sleeve_a = None`; return `{"opened": opened}` at all three sites; docstring to equity/crypto only. `mark_to_market`: delete the `execution == "live"` guard (deferred from Task 3) and replace the `_mtm_prediction` dispatch with a counted guard:
  ```python
      today = datetime.strptime(today_str, "%Y-%m-%d").date()
      retired_open = 0
      for p in book["positions"]:
          if p["status"] != "open":
              continue
          if p.get("asset_class") == "prediction":
              # Retired venue (2026-09-11): no price source exists for this class.
              # scripts/retire_prediction_rows.py closes these; until it has run
              # they are left exactly as they are, and counted so the skip is
              # attributable rather than silent.
              retired_open += 1
              continue
          ...
      if retired_open:
          log.warning(
              f"{retired_open} prediction row(s) still open — the venue is retired; "
              "run scripts/retire_prediction_rows.py"
          )
      return book
  ```
  `BOOK_LOCK_TIMEOUT` comment (31–35) → `# 120s: mode_paper holds the lock across quote fetches for every actionable signal.` Docstrings at 565–577, 1047–1066, 2096–2131: delete the prediction sentences.

- [ ] **Step 5: brief.py.** Delete `pred_name, pred_title` from the validation import and `_BUTTON_NAME_CAP`. `_close_picker_render`: `label = tkr`, delete the branch and its comment. `_close_ticker`: before the close loop,
  ```python
          retired = [p for p in matches if p.get("asset_class") == "prediction"]
          if retired:
              telegram_send(
                  f"⚠️ {html.escape(tkr)} is a prediction-market row and the venue is "
                  "retired; it cannot be priced. Run scripts/retire_prediction_rows.py "
                  "on the host to close it at its last mark."
              )
              return
  ```
  `/watch`: `parts[0] in ("equity", "crypto")`; error → `telegram_send(f"⚠️ Couldn't resolve <b>{html.escape(token)}</b>")`. HELP_TEXT :742 drop the prediction example. `_pos_ticker` docstring: drop "e.g. prediction markets". `/positions`:
  ```python
              by_class: dict[str, list[str]] = {}
              retired = 0
              for p in opens:
                  if p.get("asset_class") == "prediction":
                      retired += 1  # venue retired; see scripts/retire_prediction_rows.py
                      continue
                  label = html.escape(p.get("ticker") or p.get("instrument", ""))
                  mark = price_position(p)
                  if mark is None:
                      line = f"  – {label}: mark —"
                  else:
                      ret = _signal_return(p["direction"], p["entry_price"], mark)
                      line = f"  – {label}: {100 * ret:+.1f}%"
                  by_class.setdefault(p.get("asset_class", "equity"), []).append(line)
              lines = ["<b>📂 Open positions</b>"]
              for ac in sorted(by_class):  # every class that has rows, index included
                  lines.append(f"<b>{ac}</b>")
                  lines.extend(by_class[ac])
              if retired:
                  lines.append(
                      f"<i>{retired} prediction row(s) awaiting retirement — "
                      "run scripts/retire_prediction_rows.py</i>"
                  )
  ```
  `mode_collect` (~3486–3491): comment → quote-provider / Claude failure; call → `daily_trade_message(book, today)`.

- [ ] **Step 6: validation.py.** Delete `_PRED_NAME_CAP`, `pred_title`, `pred_name`, `_pred_handle`, `_pred_lines`. `_DIMENSIONS` drops `"play_type"`; `_ASSET_CLASSES = ("equity", "crypto")`. `aggregate_performance`:
  ```python
      closed = [
          p
          for p in book.get("positions", [])
          if p.get("status") == "closed"
          # Retired 2026-09-11: live rows measured the venue's pricing, not the
          # strategy, and prediction is a class the model can no longer emit.
          # Both stay in the book as record and count nowhere.
          and p.get("execution", "paper") != "live"
          and p.get("asset_class") != "prediction"
      ]
  ```
  `daily_trade_message(book, today)`:
  ```python
  def daily_trade_message(book: dict, today: str) -> str:
      """Unified daily trade message (Telegram-HTML). Pure — uses last-known marks.

      Sections, each omitted when empty; returns "" when there is nothing to say.
      Marks are last-known (refreshed by the weekly mark-to-market), not re-priced
      here, to keep the collect path light.
      """
      positions = book.get("positions", [])
      opened = [p for p in positions if p.get("opened") == today]
      open_now = [p for p in positions if p.get("status") == "open"]
      if not (opened or open_now):
          return ""

      lines = ["<b>📈 TRADE UPDATE</b>"]
      if opened:
          lines.append("<b>Opened today</b>")
          for p in opened:
              lines.append(
                  f"  • {p['ticker']} ({p['asset_class']}) {p['direction']} "
                  f"@ {p['entry_price']:g}"
              )
      if open_now:
          lines.append(f"<b>Open positions ({len(open_now)})</b>")
          for p in open_now:
              mark = p.get("last_mark")
              mstr = f"{100 * mark['return']:+.1f}%" if mark else "—"
              lines.append(
                  f"  • {p['ticker']} ({p['asset_class']}) {p['direction']}: {mstr}"
              )
      return "\n".join(lines)
  ```
  `ruff check validation.py`: delete `import html` if unused.

- [ ] **Step 7: common.py, compose, docstrings, tests.** Delete `POLYGRAM_EMAIL`/`POLYGRAM_PASSWORD` + comment (126–129), knobs `HAIRCUT_BPS_PREDICTION`, `VOL_FLOOR_PREDICTION`, the haircut comment at 208–209; compose: delete the two `POLYGRAM_*` lines. `enrichment/config.py:10`: `config.PG_A_ENABLED` → `config.BRIEF_MEMORY_ENABLED`. `comprehend.py:690`: `Substring matching is a recorded failure elsewhere in this repo: MU matched "Musk"`. Reword the venue out of three test-side prose hits — `tests/test_comprehend_matcher.py:3,4,47` and `tests/test_locking.py:145` (`oh4`'s acceptance forbids `polygram` anywhere under `tests/`) — and `tests/conftest.py:87` (a comment naming `PG_A_ENABLED`; use `BRIEF_MEMORY_ENABLED`). Apply every test edit from Step 1. Delete `tests/test_prediction.py`.

- [ ] **Step 8: Verify.** Acceptance is a `git grep` pair (tracked files only — the working tree holds gitignored `.playwright-mcp/` and `from-server/` that a plain `grep -r .` sweeps), with `PG_` case-SENSITIVE so `pg_tables`/`pg_sleep`/`pg_try_advisory_lock` do not match:
  ```sh
  git grep -n -i -E "polygram|prediction|play_type|entry_spread|pred_" -- '*.py' '*.yml' Dockerfile \
    | grep -v "^docs/\|claim_verify.py:180\|brief_memory.py\|thesis_claims"
  git grep -n -E "PG_[A-Z]" -- '*.py' '*.yml' Dockerfile | grep -v "^docs/"
  ```
  Pre-registered residual for the first command — every line must come from one of: `scripts/retire_prediction_rows.py`; `tests/test_retire_prediction_rows.py`; the `mark_to_market` guard and its test; the `aggregate_performance` clause and its two tests; the `_close_ticker` / `/positions` retired branches and the `/close` test; `tests/test_validation.py`'s `_closed(...)` call sites that pass `"prediction"`; `comprehend.py:690`; `tests/test_gold_set.py:149` ("a positive prediction", English). The second command must print **nothing**. Anything outside the residual is a miss — fix it, never extend the list. Then mutation-check the tolerance tests: revert the `aggregate_performance` prediction clause → exactly 1 failure; revert the `mark_to_market` guard → exactly 1 failure. Full gate; 0 failed; `def test_` delta = −(32 remaining in test_prediction + 13 named) + 5 = **−40**.

- [ ] **Step 9: Commit.** "refactor(trading): remove the paper prediction sleeve and every prediction branch".

---

### Task 5: README, compose comments, docs banners

**Files:**
- Modify: `README.md` (78, 159, 165, 170, 225, 234, 265, 334–339, 401, 406), the eight `docs/superpowers/{specs,plans}/` polygram/prediction/roadmap docs, `docs/superpowers/specs/2026-08-29-knowledge-base-architecture-design.md:475,496,568`, `docs/2026-09-11-live-buy-pricing.md:3`

- [ ] **Step 1: README.** 78: drop the prediction parenthetical. 159: "opened today and a summary of open positions". 165: drop "prediction → a naive `0`". 170: drop `play_type`. 225: drop the `PG_*` family. 234: example knob → `BRIEF_MEMORY_ENABLED`. 265: delete the `HAIRCUT_BPS_PREDICTION` row. 334–339: delete the `pgdiag` command and paragraph. 401: "equity + crypto". 406: delete `polygram_token.json`.
- [ ] **Step 2: Banners.** Under the H1 of each superpowers doc:
  ```
  > **RETIRED 2026-09-11.** Prediction-market trading was removed from news-brief (bead `news-brief-oh4`; record in `docs/2026-09-11-prediction-trading-retired.md`). Kept as history; it no longer describes the code.
  ```
  KB spec: `**PolyGram: unchanged.** Live, working, out of scope here` → `**PolyGram: retired 2026-09-11** (docs/2026-09-11-prediction-trading-retired.md)`, and "(retired)" on the two component-list mentions. `docs/2026-09-11-live-buy-pricing.md:3`: "(closed 2026-09-11; repairs applied, then the whole feature retired — see 2026-09-11-prediction-trading-retired.md)".
- [ ] **Step 3: Verify + commit.** `grep -n -i "polygram\|pgdiag\|PG_A\|prediction" README.md` → only `/thesis` lines. Commit "docs: retire prediction-market trading from the README and mark its specs historical".

---

### Task 6: Retirement record, beads, memory, host runbook, push

**Files:**
- Create: `docs/2026-09-11-prediction-trading-retired.md`
- Beads: close `rqu`, `4du`, `5qb`, `8fy`, `35p`, `7ch`; note `nyy.5` (its "trading monitor" scope loses PolyGram); file one P3 task "gate/`_ASSET_CLASSES` has no `index` row" (the review's c.3 — out of scope here, must not be lost); leave `oh4` open until the host runbook has run
- Memory: `polygram-live-trading-spec.md` (append RETIRED), `MEMORY.md` — SHORTEN, not extend, the index lines for `polygram-live-trading-spec`, `polygram-candidate-search-fix`, `commodity-signals-are-index-class`, and the roadmap entry so none presents prediction as current

- [ ] **Step 1: Record.** Sections: *Decision* (operator, 2026-09-11; on-chain reading 2.02325 shares for $1.73 against a $2.06 debit and a "~2%" fee page; Polymarket's CLOB unreachable from the jurisdiction; a Polygon wallet cannot get CLOB fills because matching is off-chain; "not a bug, a business model"). *What was removed* (module list from Tasks 3–4; `py -m pytest --collect-only -q | tail -1` before → after, REAL numbers). *What was kept and why* (`/thesis` annotation; `index` class; historical rows; exclusion of both retired populations from every aggregate; the counted `mark_to_market` guard; retired rows scored at their last mark, mirroring the deleted branch). *Host runbook*:
  ```sh
  # 0. Preconditions were MEASURED before the push (Task 6 Step 4): the three
  #    PG_*_ENABLED rows read 0, HAIRCUT_BPS_PREDICTION is 200 or absent, and the
  #    book had 0 open live rows. If any of that has changed since, stop.
  # 1. Deploy the commits (whatever the usual pull/build/up is here).
  # 2. Retire the open paper rows — dry run first; expect "N row(s) to retire, M never marked"
  #    where N is what the host book holds (the 08-29 local copy said 25 / 2; the host has moved on):
  docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json
  docker compose run --rm --entrypoint python newsbrief scripts/retire_prediction_rows.py /app/logs/paper/book.json --apply
  # 3. Optional tidy — nothing reads these any more:
  #    remove PG_* and POLYGRAM_* lines from the host docker-compose.yml and .env
  #    DELETE FROM settings WHERE key LIKE 'PG\_%' ESCAPE '\';
  #    rm /app/logs/paper/polygram_token.json /app/logs/thesis_log.json
  ```
  with: `REFUSED: ... live row(s) still open` means the plan's premise is false — stop and look. *Lessons* (one paragraph: the fixture that agreed with the formula; the options that presupposed the answer; a displayed price is not evidence of a fill; a red-team with fresh context found three green-commit failures a self-review had not).
- [ ] **Step 2: Beads.** `bd close news-brief-rqu news-brief-4du --reason="Decided 2026-09-11: polygram dropped entirely, both sleeves removed (news-brief-oh4). Measured before push: <paste the settings query output and the open-live-rows line>"`; `bd close news-brief-5qb news-brief-8fy news-brief-35p news-brief-7ch --reason="Moot: the live book and its code were retired 2026-09-11 (news-brief-oh4)"`; `bd update news-brief-nyy.5 --notes="2026-09-11: PolyGram positions/exposure/caps are out of scope; prediction trading retired (oh4)"`; `bd create --title="Gate and /positions have no index row" --type=task --priority=3 --description="mode_paper opens index rows; validation._ASSET_CLASSES has no gate row for them and /positions only gained them in oh4 via sorted(by_class). Decide whether index belongs in the go-live gate."`. `bd update news-brief-oh4 --notes="Code removed and pushed <sha>; CLOSE WHEN the host dry run has printed 'N row(s) to retire' and --apply has run."`
- [ ] **Step 3: Memory.** Prepend (top of file, not the end) to `polygram-live-trading-spec.md`: `**RETIRED 2026-09-11.** All prediction-market code removed (oh4). Do not re-propose polygram, Sleeve A/B, or a Polymarket build: the CLOB is unreachable from the operator's jurisdiction and a bare Polygon wallet cannot get fills. Historical rows stay in book.json (close_reason venue_retired / settled / manual / settlement), excluded from every aggregate.` Replace the four `MEMORY.md` index lines with ONE-LINE retired markers each (the index is over its size limit — shorten, never append).
- [ ] **Step 4: OPERATOR GATE — measured, before anything is pushed.** The worker STOPS here and asks the operator to run, on the host, and paste the output:
  ```sh
  docker compose exec -T postgres psql -U newsbrief -d newsbrief -c \
    "SELECT key, value FROM settings WHERE key IN ('PG_LIVE_ENABLED','PG_A_ENABLED','PG_B_ENABLED','HAIRCUT_BPS_PREDICTION')"
  docker compose run --rm --entrypoint python newsbrief -c "import json;b=json.load(open('/app/logs/paper/book.json'));print(sum(p.get('execution')=='live' and p.get('status')=='open' for p in b['positions']),'open live rows')"
  ```
  Required: the three `*_ENABLED` rows read false/0 (if any reads 1, the operator upserts it to 0 with the documented command and re-reads), `HAIRCUT_BPS_PREDICTION` is 200 or absent (the retire script hard-codes 0.02; any other value means its scoring would not mirror what production did), and `0 open live rows`. The pasted output goes into `rqu`'s close reason — that bead's acceptance is "a settings-row decision recorded with the observation behind it", and this is the observation. Bead `rqu` said `PG_A_ENABLED is still 1` on the morning of 2026-09-11; the operator reported flipping it later that day; neither is a measurement until this query runs.

- [ ] **Step 5: Commit + push.** Only after Step 4's paste. `git add docs/2026-09-11-prediction-trading-retired.md` — "docs: record the retirement of prediction-market trading". `git push origin main` (this triggers the image build the deploy tracks). Then the operator runs runbook steps 1–3; `oh4` closes on their word that step 2 printed its line. The record should say that the first post-deploy weekly report's "Overall" drops the ~70 previously-scored prediction rows, so a step change there is expected, not a regression.

---

## Self-review (v2)

- **Green-commit check:** Task 3 now removes every symbol that `polygram_live`'s deletion orphans (`/predict`, thesis log, the monitor block, the 16 knobs) in the same commit, and prunes `test_prediction.py`'s module import and live/sleeve tests. Task 4 removes the rest. No intermediate state imports a deleted module or reads a deleted knob.
- **Decisions applied everywhere they land:** exclusion of `prediction` from aggregates → `aggregate_performance` (Task 4 Step 6) + its test (Step 2) + the record (Task 6); scoring of retired rows → the script (Task 2) + its tests + the record.
- **Review's fix list:** 1 merged; 2 done (no `0.55` blanket rule; Postgres required); 3 step 0 + `--entrypoint` in every command; 4 both tests kept, `:71` edit added, allow-list written; 5 recorded; 6 count re-derived from the tree in Task 6.
- **Type consistency:** `plan(book, *, today)` in Task 2 matches every call in its tests and in `main`; `daily_trade_message` keeps `sleeve_a` through Task 3 and drops it with its call site in Task 4; `mode_paper` likewise.

## Self-review (v3)

- Applied the second review's fix list 1–6: operator gate before the push (Task 6 Step 4); two tests moved to Task 3 and `pgdiag` added to the pruning patterns, pre-registration now 156 for Task 3 and −40 for Task 4; live-guard deletion moved to Task 4; `_closed` positional signature; RED reasons corrected; `test_aggregate_performance_excludes_live` on the edit list; `git grep` acceptance with a pre-registered residual; three prose hits under `tests/` reworded; `HAIRCUT_BPS_PREDICTION` row checked before the retire script runs; RETIRED line at the top of the memory file; snippets to be `ruff format`ted before commit.
- Not applied, deliberately: nothing — every item was a defect.
