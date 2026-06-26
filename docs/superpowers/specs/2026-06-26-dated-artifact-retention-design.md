# Dated-Artifact Retention Sweep — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorm complete) → ready for implementation plan
**Origin:** Deferred review finding "signals/book retention" (`newsbrief-deferred-findings`), aged into relevance — the volume now accumulates **6 families** of per-day files (incl. `claim_evidence-`/`verification-` added this session) with **zero pruning**.

## One-line

A small `retention.py` module that, at the tail of each `mode_collect`, deletes dated artifact files older than 90 days and trims `signals-log.jsonl` to the same window — fully fail-safe, env-tunable.

## Motivation

Every dated artifact written to the volume (`DATA_DIR`, default `/app/logs`) accumulates forever. Files are individually small (KB), so this is **hygiene, not a disk crisis** — which sets the design bar at *simple, conservative, and incapable of breaking a consumer*, not aggressive reclamation. The growth is real and increasing: this session's #6 added two new per-day families, and `summarize_verifications` globs an ever-growing `verification-*.json` set.

`book.json` is correctly **excluded** (bounded state, not rotatable — the original finding said so). `signals-log.jsonl` is included but via a different mechanism (it grows *internally*, line by line — a file-deletion sweep can't touch it).

## Scope decisions (locked during brainstorm)

| Fork | Decision | Why |
|---|---|---|
| Keep-window strategy | **One global window, 90 days** | Clears every read-back minimum with huge margin (90 ≫ 14 ≫ 8); one knob; over-keeping 2-day-use files costs trivial KB. Per-family tuning is YAGNI. |
| Window configurable? | **Env override `NEWSBRIEF_RETENTION_DAYS`** (default 90), resolved at call time | Matches `NEWSBRIEF_DATA_DIR` convention. `days <= 0` ⇒ disabled (off-switch folded into the one knob, no separate flag). |
| `signals-log.jsonl` | **Include — 90-day date-based line-trim** | Literally the "signals retention" half of the finding; consistency with pruning the `signals-{day}.json` snapshots it mirrors. Marginal value (slow growth, no code reader) but completes the story. |
| Where it runs | **Tail of `mode_collect`** (after the trading stage), fail-safe-wrapped | Daily heartbeat, co-located with the writes, zero new cron config. |
| `weekly/week-*.md` | **Excluded** | Tiny (~52/yr), slow, most "knowledge-like"; pruning saves ~nothing and risks losing history. Easy to add later. |

**Non-goals:** per-family windows; pruning bounded-state files; a dedicated cron mode; a dry-run mode (90-day margin + date-only targeting + keep-on-doubt make it unnecessary); HTML/JSON-aware anything.

## Read-back constraints (set the minimum keep-window; 90d clears all)

| Family | Read back by | Min keep |
|---|---|---|
| `briefs/brief-{day}.md` | `load_yesterday_brief` (1d) + weekly `load_recent_briefs(7)` | 8 |
| `data/verification-{day}.json` | `summarize_verifications` (pilot) | 14 |
| `data/source_index-{day}.json` | next collect | ~2 |
| `data/claim_evidence-{day}.json` | next collect | ~2 |
| `data/enrichment/enrichment-{day}.json` | same-day collect | ~2 |
| `signals/signals-{day}.json` | same-day paper | ~2 |

## Architecture

New top-level module **`retention.py`** — pure functions + a fail-safe orchestrator. Mirrors the focused-module pattern of `brief_memory.py` / `claim_verify.py`.

```
mode_collect (tail of the `if raw:` success path, after the trading stage):
  run_retention(today)  ──► delete dated files older than cutoff
                        └──► trim signals-log.jsonl to the window
                        └──► return {deleted, trimmed_lines}  (collect logs one line)
```

- Window resolved at call time: `run_retention(today, *, days=None)` reads `NEWSBRIEF_RETENTION_DAYS` (default 90) when `days is None`; tests pass `days` directly.
- `days <= 0` ⇒ disabled no-op, returns `{deleted: 0, trimmed_lines: 0}`.
- Wrapped in its own `try/except` in `mode_collect`, sibling to the verification/reconcile blocks.
- **`dockerfile-copy-allowlist` chore:** add `retention.py` to the Dockerfile `COPY` line, the workflow path trigger, and both ruff file lists, else runtime `ModuleNotFound` that escapes CI lint.

## Component detail

### Mechanism 1 — dated-file deletion
A list of `(directory, glob)` family specs:

| Directory | Glob |
|---|---|
| `BRIEFS_DIR` | `brief-*.md` |
| `DATA_DIR` | `source_index-*.json` |
| `DATA_DIR` | `claim_evidence-*.json` |
| `DATA_DIR` | `verification-*.json` |
| `DATA_DIR / "enrichment"` | `enrichment-*.json` |
| `SIGNALS_DIR` | `signals-*.json` |

For each glob match: extract a `YYYY-MM-DD` from the filename (regex `(\d{4}-\d{2}-\d{2})`); if it parses **and** the date is strictly older than `cutoff = today − days`, delete the file. **A filename yielding no parseable date is skipped, never deleted** — the core safety rule.

### Mechanism 2 — `signals-log.jsonl` line-trim
Read all lines; keep a line if its parsed `date` field is within the window **or** the line is unparseable / has no `date` (keep-on-doubt — never silently drop a line you can't date). Atomic-rewrite via temp file + `os.replace`. No-op if the file is absent.

### Two safety properties
- `signals-*.json` does **not** match `signals-log.jsonl` (glob end-anchored on `.json`; the log ends in `.jsonl`) — *and* the log has no `YYYY-MM-DD` in its name, so the date-skip rule would spare it regardless. Double-protected.
- The sweep targets **only** these explicit date-bearing patterns. Bounded-state files (`book.json`, `brief_memory.json`, `feedback.json`, `batch_state.json`, sources/watchlist/pins) carry no date in their names ⇒ no glob matches them ⇒ structurally untouchable.

### Return value
`run_retention` returns `{deleted: int, trimmed_lines: int}` so `mode_collect` can log one line (e.g. `Retention: deleted 6 files, trimmed 3 log lines`).

## Error handling / fail-safe ladder

The brief is already delivered when this runs; hygiene must never threaten it.

| Failure | Behavior |
|---|---|
| `NEWSBRIEF_RETENTION_DAYS` unset/invalid | Default 90; non-int → caught, fall back to 90 (log warning). |
| `days <= 0` | Disabled no-op; returns zero summary. |
| Directory absent | Family contributes nothing; skip. |
| File date won't parse | Skip that file (never delete undateable). |
| Single `unlink` fails (permission/race) | Per-file `try/except`, log, continue. |
| Malformed `signals-log.jsonl` line | Keep it (keep-on-doubt); never abort the trim. |
| Atomic rewrite fails mid-way | `os.replace` is atomic; original log intact until temp fully written; on error original untouched. |
| Anything unexpected in `run_retention` | Outer `try/except` → log, return accrued summary. `mode_collect` also wraps the call. |

Per-family and per-file isolation: the orchestrator loops families in independent `try/except` blocks so one failure never aborts the others.

## Testing (TDD, mirrors `test_claim_verify.py`; deterministic, no network)

- `_file_date` — extracts `YYYY-MM-DD` from each family's filename; `None` for undateable names (incl. `signals-log.jsonl`).
- file-deletion — `tmp_path` dirs with files dated across the boundary: deletes strictly-older, keeps within-window, **keeps undateable**; per-family globs hit only intended files; `signals-*.json` does not match `signals-log.jsonl`.
- bounded-state guard — `book.json` / `feedback.json` in the dir is never deleted.
- line-trim — old lines dropped, recent kept, malformed/no-date lines kept, file remains valid JSON-lines, file-absent no-op.
- env override — `days` from `NEWSBRIEF_RETENTION_DAYS`; invalid → 90; `days <= 0` → disabled no-op.
- fail-safe — injected error / unwritable target ⇒ `run_retention` never raises; returns a summary.
- summary counts (`deleted`, `trimmed_lines`) correct.

All tests use `tmp_path` + `monkeypatch` on the dir constants (same pattern Tasks 1–5 of #6 used for `DATA_DIR`). No pandas ⇒ no `importorskip`. Full gate per `brief-local-run`: `ruff check` + `ruff format --check` + `pytest` (stage every reformatted file).

## Rollout

1. Build + tests green; commit straight to `main` (solo repo).
2. Deploy (Docker) — may batch with the held #4/#5a/#5b/#6 deploy, per user.
3. Default 90 days is active immediately on deploy (no flag). Optionally set `NEWSBRIEF_RETENTION_DAYS` high (e.g. `3650`) on first deploy to watch the logs prove what it *would* delete, then tighten to 90.

## References

- Deferred finding: `newsbrief-deferred-findings` ("signals/book retention", "Unbounded growth").
- Dir constants: `common.py` (`DATA_DIR`, `SIGNALS_DIR`), `brief.py` (`BRIEFS_DIR`, `WEEKLY_DIR`).
- Lesson applied: `dockerfile-copy-allowlist` (new top-level module = 3 updates).
- Sibling fail-safe pattern: the post-deliver verification/reconcile blocks in `mode_collect`.
