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
  Rationale: bd cross-machine sync requires a PINNED `dolt` binary on every machine plus
  `bd bootstrap` per clone. This override is sanctioned by the Beads block itself:
  explicit repository instructions take precedence over it, and it is task-tracking
  guidance, not a memory mandate.

## Memory (read this before writing one)

Corrected 2026-09-04 (`news-brief-iad`). The previous version of this file claimed the
corpus was "hydrated by the hydrate-memory SessionStart hook" and "already travels with
the repo". **Neither was true when written** — no such hook existed in either scope, and
the committed copy was an orphaned snapshot five days behind. That is `metadata-is-not-state`:
this file states intent, never what runs. What follows describes the mechanism that now
actually exists; verify it in `.claude/settings.json` rather than trusting this paragraph.

There are **two** copies of the corpus, and which one is authoritative depends on the machine:

- `~/.claude/projects/<key>/memory/` — the **live** one. The auto-memory system reads and
  writes here, and its `MEMORY.md` loads automatically. **Write learnings here.**
- `.claude/memory/` — the **committed** copy, 68 entries, carried in git.

`.claude/hooks/hydrate-memory.sh` runs at SessionStart. Where the live corpus exists it
prints **nothing**, because the index is already loaded and a second copy is noise. Where it
does not — a cloud session, a second machine — it injects the committed `MEMORY.md` and tells
you to read `.claude/memory/<name>.md` for an entry that looks relevant. It deliberately does
**not** copy files into the live corpus: the project key is derived from the working
directory, its form on a Linux host is unverified, and a wrong key writes where nothing reads.

Memories written in a session do **not** reach git on their own. `scripts/flush-memory.sh`
copies live → committed and then stops, without staging or committing.

**That last part is deliberate, and it is the thing to remember: this repository is PUBLIC.**
Every memory written here is a publishing candidate. The 2026-09-04 review of 21 files found
no credentials, but did find an operational map of the deploy host and a behavioural profile
of the author — material that wants a human glance, not a hook. Review
`git diff -- .claude/memory/` before committing, and see `live-state-on-deploy-host.md`.

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
