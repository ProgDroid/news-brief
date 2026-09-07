---
name: brief-local-run
description: How to import/run brief.py locally — now import-safe; tests exist under tests/
metadata: 
  node_type: memory
  type: project
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
---

Since the 2026-06-09 testability refactor, `import brief` works with NO env vars and NO `/app/logs`: config is read with `.get()` (validated only in `__main__`), and the data root is `NEWSBRIEF_DATA_DIR` (default `/app/logs`; the file log handler degrades to console-only if the dir can't be created). Set `NEWSBRIEF_DATA_DIR` to a temp dir before importing so state files don't land in a cwd-drive `\app\logs`.

**Why:** the old dummy-env + mkdir dance is obsolete; tests/conftest.py already sets `NEWSBRIEF_DATA_DIR` to a tempdir before import.

**How to apply:** the FULL pre-push gate (match CI exactly, or it fails after you push) is THREE commands, not just pytest:
`python -m ruff check brief.py common.py trading.py tests` ·
`python -m ruff format --check brief.py common.py trading.py tests` ·
`python -m pytest tests -q`.
Deps: `pip install -r requirements.txt -r requirements-dev.txt` (ruff is pinned there, 0.14.14). The local interpreter here is Python 3.14 while CI + the Docker image are 3.12 — fine for this code, but install ruff into whatever `python` resolves to. **CORRECTED 2026-09-01: run it from the Bash tool with the `py` launcher** — `py -m pytest -q`, `py -m ruff check .` — which the winpty alias does not wrap, so it returns the real exit code. The PowerShell detour in [[python-via-powershell]] is no longer needed for this.

**3. Since Epic 7 (2026-09-01), `pytest` alone reports green with the ENTIRE database
layer unexecuted.** `tests/test_db.py` (and any DB-backed module) skips on
`db.is_configured()`, so with nothing configured the suite says `1101 passed` and has
told you nothing about `db.py`, the migrations, or the run ledger. A skip is not a pass,
and the count moving is the only proof the probe can return anything. Start one and
confirm the runs:
`docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine`
then `export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"`
and check `pytest tests/test_db.py -q` reports runs rather than `skipped`. The container
is often already up from an earlier session — `docker ps` before starting a second one.
Also now in the repo's own `CLAUDE.md` Build & Test section, so a fresh clone gets it.

**Verification gotchas (both bit us 2026-06-13, three red CI runs):**
1. **pytest-only is NOT enough** — CI also runs `ruff check` + `ruff format --check`. A plan/subagent that verifies with pytest alone will pass locally and fail CI on F401 unused imports (common after moving code between modules) or E402 (a mid-file `from x import` — keep all module-level imports at the top). Bake the two ruff commands into every plan's verification steps and every implementer subagent's instructions.
2. **`ruff format --check` passing locally does NOT predict CI** — it checks your WORKING TREE; CI checks the COMMITTED tree. `ruff format` edits files in place, so after running it you must `git add` EVERY reformatted file (check `git status` before committing). An unstaged reformat passes local `--check` and fails CI.

CI also gates the Docker publish on this suite, and `build-and-push` runs the real `docker build` — so a green CI run IS the Docker-image test (Docker Desktop is often not running locally). For live-endpoint checks, import brief and loop `fetch_rss` over `RSS_FEEDS` (requests, 20s timeout). [[formatter-owns-style]]

**E402 and F401 are a PAIR, and only one move satisfies both (2026-09-03).** Trying to dodge
E402 by declaring every import a module will eventually need, up front, buys three F401s
instead — and F401 is the one that fails a gate while the module is still half-built. E402
constrains an import's POSITION, not when it is added, so the correct move for a new import
at any point is *edit the top import block*, never append lower down and never pre-declare.
Ruff has no config in this repo, so both rules are on by default.

**Adding a `Schedule` changes the expected test count by more than the tests you wrote.**
`tests/test_scheduler.py:259` parametrizes over `scheduler.SCHEDULES`, so a 6th schedule
silently adds a case. Any pre-registered absolute count for a scheduling change must account
for it — and the way to settle a count disagreement is `pytest --collect-only -q`, not a grep,
which can return the right number for the wrong reason. See [[tests-asserting-less-than-their-name]].

## 2026-09-04 — a vanished Postgres HANGS the suite; it does not fail it

Docker Desktop's daemon died mid-run, taking the test container with it. The suite did not error
— it **hung**, indefinitely, on the first DB-touching test of each file, having emitted a few
teardown errors on the way down that looked exactly like defects in whatever had just been
edited. Cost ~25 minutes of misattributed debugging.

Two causes compound. `db.is_configured()` reads `DATABASE_URL`, so it answers "configured" for a
server that is **gone** — the skipif guard does not fire and the tests run. And the `clean_db` /
`store` / `state_store` fixtures call `db.connect()` with **no `connect_timeout`**, unlike the
production read paths (`brief._jobs_render`, the supervisor) which pass one. libpq then waits
forever. Filed as `news-brief-5qd`.

**Triage rule, before suspecting your own edit:** a pytest run that stops making progress is an
infrastructure question first. `docker ps` — if the daemon is unreachable the answer is there in
one command, and the fix is `"/c/Program Files/Docker/Docker/Docker Desktop.exe" &`, wait for
`docker ps` to answer, then restart the container and **wait for readiness** rather than guessing:

```
until docker exec nb-test-pg pg_isready -U newsbrief > /dev/null 2>&1; do sleep 2; done
```

**Tell:** progress dots stop at a stable percentage, `E`s appear in a short run, and the file it
stops in is one you did not touch. A hang is not a slow test, and it is not your diff.

**The suite count moved again**: 1457 passing as of `9264278` (was 1410 at the start of the same
session). Report the ABSOLUTE number and reconcile any gap — predicting 1462 and seeing 1457 was
me double-counting tests already in the baseline, not a missing test.

## A module-level DB skipmark hides non-DB tests from CI (2026-09-07)

Every `tests/test_comprehend_*.py` file opens with
`pytestmark = pytest.mark.skipif(not db.is_configured(), ...)`, which skips the **whole module**.
So a test that needs no database, dropped into one of those files for topical tidiness, never runs
in CI — where no `DATABASE_URL` is set. It passes locally (you have a container up) and is silently
absent from the gate that matters.

**Rule: place a test by what it NEEDS, not by what it is ABOUT.** The network-seam tests for
`comprehend` went into a new `tests/test_comprehend_network.py` with no skipmark, precisely so the
fix they pin is exercised on every CI run. Before adding to an existing test file, check the top of
it for a `pytestmark`.

Same family as "a skip is not a pass" above — and the count is the check: this session went
1554 → 1571 with **0 skipped**, and the +17 was reconciled against the tests actually written
(10 + 5 + 2) rather than assumed.
