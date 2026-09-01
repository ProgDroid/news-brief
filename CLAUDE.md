# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd` for WORK STATE only: issues, dependencies, what is ready next.
- **REPOSITORY OVERRIDE (2026-08-27): memory does NOT move to `bd remember` in this repo.**
  `.claude/memory/` remains the durable memory corpus, hydrated by the hydrate-memory
  SessionStart hook and carried in git. Rationale: bd cross-machine sync requires a
  PINNED `dolt` binary on every machine plus `bd bootstrap` per clone, whereas the
  file corpus already travels with the repo and needs nothing installed. This override
  is sanctioned by the Beads block itself: explicit repository instructions take
  precedence over it, and it is task-tracking guidance, not a memory mandate.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

The pre-push gate is **three** commands, not just pytest — CI runs all three, and
`ruff format` edits in place, so `git add` every file it touches or CI fails on the
committed tree while your working tree looks clean.

```bash
ruff check .
ruff format --check .
pytest -q
```

**`pytest` alone reports green with the entire database layer unexecuted.** The
DB-backed modules skip on `db.is_configured()`, so with no database configured the
suite passes and says nothing about `db.py`, the migrations, or the run ledger — a
skip is not a pass. Export a connection first, and check the run count moved:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief \
  -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
pytest tests/test_db.py -q   # must report runs, not "skipped"
```

Local verification of the full stack is **`docker compose config` only**. Never
`docker compose up -d` here: it starts a second Telegram `getUpdates` consumer and
409s the live bot.

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
