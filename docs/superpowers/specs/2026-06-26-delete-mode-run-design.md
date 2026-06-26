# Delete `mode_run` — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorm complete) → ready for implementation plan
**Origin:** Deferred review finding "`mode_collect`/`mode_run` collect-body merge (large orchestration fns, behaviour-change risk)" (`newsbrief-deferred-findings`).

## Decision

**Delete `mode_run` rather than merge it.** Investigation showed `mode_run` is a *drifted, unused* partial clone of `mode_collect`'s post-poll body — it lags ~5 steps (enrichment annotation, brief-memory reconcile, claim-verify, the daily trade-update, retention) and uses the opposite `clear_batch_state`/trading order. It is the no-arg default entrypoint but is exercised by **nothing**: the host runs explicit `submit`/`collect`/`weekly`/`monitor`, no test or CI references it, and the user has never run it manually.

A full-parity merge would refactor `mode_collect` — the production brief-delivery path — solely to make dead code faithful. That spends risk on the most critical function for zero production benefit. Deleting removes the duplication *at its source* with **zero changes to `mode_collect`**.

(Considered and rejected: parity merge via a shared `_handle_collected_brief` helper — only worth it if the user wanted a faithful local end-to-end smoke test, which they do not; won't-fix — leaves misleading "test" dead code in place.)

## Changes (all in `brief.py` + `README.md`)

1. **Delete the `mode_run` function** (`brief.py:2680-2703`).
2. **Dispatch dict:** remove the `"run": mode_run,` entry (`brief.py:2816`).
3. **No-arg default:** change `mode = sys.argv[1] if len(sys.argv) > 1 else "run"` to `else ""`, so bare `python brief.py` falls through to the existing usage branch (`print("Usage: ...")` + `sys.exit(1)`) instead of silently running a full pipeline.
4. **Usage string** (`brief.py:2832`): `Usage: brief.py [submit|collect|weekly|paper|commands|monitor]` — drops `run`, *adds* `paper` (a real dispatch mode previously omitted from the string).
5. **README:** remove the `run` table row (line 49) and the `docker run ... newsbrief run` sync-test example (line 222).

## Explicitly unchanged

`mode_collect` is **untouched** — no helper extraction, no body move. This is the entire risk argument for choosing delete over merge.

## Verification — no new test (dead-code deletion)

Adding a test here is negative value: there is no behavior to characterize, and an `assert not hasattr(brief, "mode_run")` test merely ossifies an absence. The correct safety net is:

- **Suite stays green** unchanged — proves nothing depended on `mode_run` (already confirmed: zero importers/callers across `tests/`, `.github/`, and the codebase).
- **ruff clean** — catches any dangling reference to the deleted symbol.
- **Smoke:** `import brief` succeeds and `'mode_run' not in dir(brief)`; bare `python brief.py` (no args) prints the usage line and exits 1 (not a pipeline run).

Full gate per `brief-local-run`: `ruff check` + `ruff format --check` + `pytest`.

## Rollout

Commit straight to `main` (solo repo). Pure code/doc change, no env or deploy semantics; joins the held batch (#4/#5a/#5b/#6/retention) or deploys independently — user's call. No host action needed.

## References

- `newsbrief-deferred-findings` (the merge finding).
- Dispatch block: `brief.py:2803-2833` (`if __name__ == "__main__":`).
- `brief-local-run` memory (local validation is `ruff + pytest`, not `run`).
