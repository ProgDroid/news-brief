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
Deps: `pip install -r requirements.txt -r requirements-dev.txt` (ruff is pinned there, 0.14.14). The local interpreter here is Python 3.14 while CI + the Docker image are 3.12 — fine for this code, but install ruff into whatever `python` resolves to. The Bash tool can't run python ("stdin is not a tty") — use the PowerShell tool ([[python-via-powershell]]).

**Verification gotchas (both bit us 2026-06-13, three red CI runs):**
1. **pytest-only is NOT enough** — CI also runs `ruff check` + `ruff format --check`. A plan/subagent that verifies with pytest alone will pass locally and fail CI on F401 unused imports (common after moving code between modules) or E402 (a mid-file `from x import` — keep all module-level imports at the top). Bake the two ruff commands into every plan's verification steps and every implementer subagent's instructions.
2. **`ruff format --check` passing locally does NOT predict CI** — it checks your WORKING TREE; CI checks the COMMITTED tree. `ruff format` edits files in place, so after running it you must `git add` EVERY reformatted file (check `git status` before committing). An unstaged reformat passes local `--check` and fails CI.

CI also gates the Docker publish on this suite, and `build-and-push` runs the real `docker build` — so a green CI run IS the Docker-image test (Docker Desktop is often not running locally). For live-endpoint checks, import brief and loop `fetch_rss` over `RSS_FEEDS` (requests, 20s timeout). [[formatter-owns-style]]
