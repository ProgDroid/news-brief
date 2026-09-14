# Bead cleanup after the prediction-trading retirement

**Date:** 2026-09-14 · Companion to `docs/2026-09-11-prediction-trading-retired.md`

The retirement (`2507cad`…`73b97df`, 2026-09-11) removed the venue client, both
sleeves and the paper prediction sleeve, but **no bead was closed with it**. The
tracked export `.beads/issues.jsonl` was last written 2026-09-10, so anything
reading it — `bd ready`, a fresh session, the next person — is told the top
priority is a **p0 about `cost_basis` units in a file that no longer exists**.

This lists what to close and why. The commands are here rather than run because
this session has no `bd` binary and no Dolt database (`.beads/dolt/` is
gitignored and `refs/dolt/*` is absent from the remote), so the tracker cannot
be reached from a cloud box at all.

---

## Close: the condition is now unreachable in code

`polygram_live.py` is deleted; `grep -ril polygram *.py` finds only
`scripts/retire_prediction_rows.py`. Nothing can open, monitor, or sell a live
position.

```sh
bd close news-brief-8fy --reason "Moot: prediction-market trading retired 2026-09-11. polygram_live.py and the live monitor are deleted, so the unmatchable position can no longer be logged, swept or sold. See docs/2026-09-11-prediction-trading-retired.md."
bd close news-brief-5qb --reason "Moot: no live position can exist after the 2026-09-11 retirement. The alerting gap it describes is real but has no subject; if live execution ever returns, re-file against the reference alert contract (capture.liveness)."
bd close news-brief-qiz --reason "Moot: backfill_settled and the whole polygram_live.py venue layer were deleted on 2026-09-11. The venue_key/raw-tuple mismatch has no code to be wrong in."
bd close news-brief-p3v --reason "Superseded: the friction it measured is the venue's buy rule, root-caused in docs/2026-09-11-live-buy-pricing.md and the reason the venue was dropped. Recorded in the retirement write-up; nothing left to act on."
```

## Close: fixed in code, with a host step that is no longer load-bearing

Both had their fix land **before** the retirement, each with a repair script that
the retirement then deleted. Whether those scripts ever ran against the host book
cannot be checked from here — but it no longer changes any reported number:
`validation.aggregate_performance` (validation.py:62-63) now excludes **every**
row with `execution == "live"` and every `asset_class == "prediction"` row from
the overall and per-dimension stats. The rows stay in `book.json` as record and
count nowhere.

```sh
bd close news-brief-rhg --reason "Fixed in 80d112d (entry_price = amount/shares, cost_basis = amount + total_fee) before the 2026-09-11 retirement. scripts/repair_live_cost_basis.py re-based the host rows and was removed with the venue layer. Moot either way: aggregate_performance excludes all live rows, so no reported figure reads a live cost_basis."
bd close news-brief-9tq --reason "Addressed in b136f75: six of the seven resolved at the venue and were valued from the measured payout (shares/2.06 - 1). The seventh stays unvalued. Moot either way: aggregate_performance excludes all live rows."
```

**If the host book matters to you for its own sake** — not for any aggregate —
note that both repair scripts are gone from the tree and would have to be
recovered from history (`git show 80d112d:scripts/repair_live_cost_basis.py`).
That is the only reason to touch it; nothing reads those fields now.

## Keep open: these survived the retirement

- **`news-brief-7dy`** (Stage B: performance-attribution firewall) — equity,
  crypto and index trading all remain. Unaffected.
- **`news-brief-1u2` / `1u2.1` / `1u2.2`** (Epic 6, deferred) — the `/thesis`
  annotation feature was explicitly **kept** (it is thesis-per-position notes on
  any asset class). `1u2.1` "re-found the paper book on theses" wants a scope
  re-read, since prediction theses and the thesis log are gone but the feature
  is not. **Re-read, don't close.**

## One bead to file

The retirement's host runbook (deploy, then
`scripts/retire_prediction_rows.py --apply`) has no bead tracking whether it ran.
Until it does, `mark_to_market` logs *"N prediction row(s) still open — run
scripts/retire_prediction_rows.py"* on every cycle.

```sh
bd create "Confirm the prediction-retirement host runbook ran" -t task -p 2 \
  -d "docs/2026-09-11-prediction-trading-retired.md gives a host runbook: deploy the retirement commits, then run scripts/retire_prediction_rows.py (dry run, then --apply) against /app/logs/paper/book.json. Nothing records whether it happened. The tell that it has NOT: mark_to_market logs 'N prediction row(s) still open -- run scripts/retire_prediction_rows.py' every cycle, and /close and /positions report the same. REFUSED: ... live row(s) still open means the plan's premise is false -- stop and look, do not add --force."
```

## Why the export was believed in the first place

`.beads/issues.jsonl` is a **passive export**, not the tracker. It is only as
fresh as the last `bd export`, and a commit that changes code does not refresh
it. A session that reads it — the only option on a machine without the Dolt DB —
is reading a snapshot, and nothing in the file says how old it is. Worth checking
`git log -1 -- .beads/issues.jsonl` against `git log -1` before trusting it.
