# Delete `mode_run` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the unused, drifted `mode_run` function and its dispatch wiring, make bare `python brief.py` print usage instead of running a pipeline, and remove its README references — without touching `mode_collect`.

**Architecture:** Pure dead-code removal. One function deleted, three small edits in the `__main__` dispatch block, two README edits. No new logic, no new tests — the existing suite staying green proves nothing depended on `mode_run`.

**Tech Stack:** Python 3, `pytest`, `ruff`.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-26-delete-mode-run-design.md`.
- **`mode_collect` is UNTOUCHED** — this is the entire risk argument for delete-over-merge. Do not extract helpers or move its body.
- **No new test** — dead-code deletion. The gate (suite green + ruff clean) plus the smoke check is the safety net.
- **Commit style:** conventional commits; commit via the **Bash tool** (PowerShell prepends a BOM to commit subjects). End messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Environment:** run python/pytest/ruff via the **PowerShell tool** (Bash errors "stdin is not a tty" for Python). PowerShell may surface Python stderr as a red "NativeCommandError" even on success — judge by the actual summary line.
- **Test gate (per `brief-local-run`):** `ruff check` + `ruff format --check` + `pytest` all pass.

## File Structure

- **Modify `brief.py`** — delete the `mode_run` function; remove its dispatch entry; change the no-arg default; fix the usage string.
- **Modify `README.md`** — remove the `run` mode table row and the `docker run ... run` sync-test example.

---

### Task 1: Delete `mode_run` and its wiring + README references

**Files:**
- Modify: `brief.py` (function at `:2680-2703`; dispatch block at `:2803-2833`)
- Modify: `README.md` (the `run` table row; the sync-test example)

- [ ] **Step 1: Delete the `mode_run` function from `brief.py`**

Remove this entire function (it sits between `mode_weekly` and `register_bot_commands_if_changed`):
```python
def mode_run():
    """Synchronous submit + collect for testing."""
    log.info("=== RUN (sync) ===")
    mode_submit()
    state = load_state() or {}
    batch_id = state.get("batch_id")
    if batch_id:
        raw = poll_batch(batch_id, max_wait_secs=3600)
        if raw:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            brief = raw.strip()
            deliver(
                brief,
                header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
                archive_path=BRIEFS_DIR / f"brief-{today}.md",
            )
            raw_signals, status = extract_signals(brief)
            signals, dropped = normalize_signals(raw_signals)
            if dropped or status != "ok":
                log.warning(f"Signals: status={status}, dropped={dropped}")
            signals = annotate_signal_sources(signals)
            save_signals(signals, today, status=status, dropped=dropped)
            mode_paper()
            clear_batch_state()
```
Leave exactly one blank line between the preceding function (`mode_weekly`) and the following one so spacing matches the file (ruff format will normalise to two blank lines between top-level defs — run it in Step 5).

- [ ] **Step 2: Remove the dispatch entry**

In the `if __name__ == "__main__":` block, change:
```python
    dispatch = {
        "submit": mode_submit,
        "collect": mode_collect,
        "weekly": mode_weekly,
        "run": mode_run,
        "commands": mode_commands,
        "paper": mode_paper,
        "monitor": mode_monitor,
    }
```
to (drop the `"run"` line):
```python
    dispatch = {
        "submit": mode_submit,
        "collect": mode_collect,
        "weekly": mode_weekly,
        "commands": mode_commands,
        "paper": mode_paper,
        "monitor": mode_monitor,
    }
```

- [ ] **Step 3: Change the no-arg default and the usage string**

Change:
```python
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
```
to:
```python
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
```
And change the usage line (in the `else:` branch):
```python
        print("Usage: brief.py [submit|collect|weekly|run|commands|monitor]")
```
to (drop `run`, add `paper`):
```python
        print("Usage: brief.py [submit|collect|weekly|paper|commands|monitor]")
```

- [ ] **Step 4: Remove the README references**

In `README.md`:
- Delete the modes-table row for `run` (reads `| \`run\` | \`submit\` + \`collect\` synchronously — for testing | — |`).
- Find the synchronous-end-to-end-test example (a shell snippet around line 222: a comment like `# Synchronous end-to-end test (submit + poll + deliver), ~30s–2min:` followed by `docker run --rm --env-file .env -v "$PWD/logs:/app/logs" newsbrief run`) and delete that comment + command pair. Read the surrounding lines first and remove only those two lines, leaving the rest of the code block and the other `docker compose run` cron examples intact.

- [ ] **Step 5: Format, lint, and run the full gate (PowerShell)**

Run and confirm all green:
```
ruff format brief.py
ruff check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
ruff format --check brief.py brief_memory.py claim_verify.py retention.py common.py trading.py enrichment tests
python -m pytest -q
```
Expected: ruff clean (no `F821`/undefined-name, no unused import — `mode_run` was the only caller of nothing exclusive, so no import becomes unused; if ruff flags a now-unused import, that's a real finding — report it). Full suite passes with the same count as before this task (~515 — nothing referenced `mode_run`).

- [ ] **Step 6: Smoke check (PowerShell)**

Run:
```
python -c "import brief; print('mode_run' not in dir(brief)); print('run' not in __import__('inspect').getsource(brief).split('dispatch = {')[1].split('}')[0])"
```
Expected: `True` then `True` (function gone; `run` no longer a dispatch key).

Then verify bare invocation prints usage and exits non-zero (it should NOT run a pipeline):
```
python brief.py; echo "exit=$LASTEXITCODE"
```
Expected: prints `Usage: brief.py [submit|collect|weekly|paper|commands|monitor]` (or a "Missing required environment variables" line if env isn't set — either is acceptable; the point is it does NOT start a RUN/submit). Must not log `=== RUN (sync) ===`.

- [ ] **Step 7: Commit**

```bash
git add brief.py README.md
git commit -F - <<'EOF'
refactor(cleanup): delete unused drifted mode_run

mode_run was an unused, drifted partial clone of mode_collect's post-poll
body (no enrichment/reconcile/verify/retention/trade-update; opposite
clear_batch_state order). Nothing ran it: host uses explicit modes, no
test/CI references it. Delete it; bare `python brief.py` now prints usage.
Usage string drops run, adds paper. mode_collect untouched.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review Notes

- **Spec coverage:** delete function (Step 1); remove dispatch entry (Step 2); no-arg default → usage (Step 3); usage string `-run +paper` (Step 3); README row + sync-test example (Step 4); `mode_collect` untouched (no step modifies it); verification = suite green + ruff + smoke (Steps 5-6). All spec points covered.
- **No-placeholder check:** every edit shows exact before/after text. The only non-literal is the README sync-test snippet, where the implementer is told to read surrounding lines and delete the specific comment+command pair — necessary because the exact whitespace/neighbours in that code fence aren't reproduced here; the two target lines are quoted.
- **Risk note:** the one thing to watch in Step 5 is whether deleting `mode_run` orphans an import that was used *only* there. `mode_run` used `mode_submit`, `load_state`, `poll_batch`, `deliver`, `extract_signals`, `normalize_signals`, `annotate_signal_sources`, `save_signals`, `mode_paper`, `clear_batch_state`, `BRIEFS_DIR`, `datetime`/`timezone` — all of which are used elsewhere in `brief.py` (notably `mode_collect`), so no import should become unused. Ruff in Step 5 is the check; if it flags one, report it rather than silently removing a shared import.
