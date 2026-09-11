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

- Use `bd` for all DURABLE task tracking: file the issue before writing the code.
  **TodoWrite is a different layer and is NOT banned here** — stated once, in
  `.beads/PRIME.md`, and again below under "Task tracking vs. execution structure".
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd` for WORK STATE only: issues, dependencies, what is ready next.
- **Memory is files, not `bd remember`.** Stated once, in `.beads/PRIME.md`, which
  overrides `bd prime`'s default output — so nothing needs rebutting here any more.

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

<!-- Added 2026-09-10, deliberately OUTSIDE the managed block, for the same reason
     the Memory section below sits out here: `bd setup claude` rewrites everything
     between the BEGIN/END markers wholesale. -->

## Task tracking vs. execution structure

The generated Beads block bans `TodoWrite`. **That ban is overridden here** — which the
block itself sanctions: *"Explicit user or orchestrator instructions override this Beads
block"*, and it calls itself *"task-tracking guidance, not permission to override"*.

They are two different layers and the ban conflates them:

| | Owns | Lifetime |
|---|---|---|
| **bd** | what work exists, dependencies, what is ready next | across sessions |
| **TodoWrite** | how a procedure gets followed — the checklist a skill hands you | within the turn |

Banning `TodoWrite` does not move that state into bd. It deletes it, and with it the
enforcement mechanism of every Superpowers skill — `superpowers:using-superpowers` says
*"If it has a checklist, create a todo per item."* Without it, `systematic-debugging`,
`test-driven-development` and `verification-before-completion` degrade from procedures
that get worked through into text that gets read.

**This is not theoretical.** Two failures here trace to it: bugs chased without ever being
filed as bugs, and `the-filter-presupposed-the-answer` (2026-09-10) — a diagnostic that
selected DB fields with `if "id" in k.lower()`, ran for a month, and never found
`position_key` because the field it was hunting is by definition the one that broke the
naming convention.

**File the issue before writing the code** — that part of the Beads rule is right and is
what earns bd its place. Then use `TodoWrite` for the procedure that does the work.

### After any `bd setup claude` or beads upgrade

The managed block is regenerated wholesale, so the in-block pointer above can silently
revert with no diff-time warning. Check after any upgrade:

```sh
sh .beads/check-block.sh     # 0 = ok, 1 = the override was clobbered, 2 = UNKNOWN
```

That check is a script rather than a grep line in this file on purpose. Two earlier
versions were written inline here and both matched their own documentation — see the
script's header comment. Verified against a deliberately reverted copy, so it is known
to discriminate rather than merely known to pass.

<!-- Moved out of the BEADS INTEGRATION block on 2026-09-06. It lived inside
     lines that `bd setup claude` owns and rewrites wholesale, and it is not part
     of bd's template, so a template change would have silently deleted it. -->

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
- `.claude/memory/` — the **committed** copy, carried in git. (No count here on purpose: the
  previous version of this file hardcoded one and it went stale. Run `ls .claude/memory/*.md`.)

`.claude/hooks/hydrate-memory.sh` runs at SessionStart. Where the live corpus exists it
prints **nothing**, because the index is already loaded and a second copy is noise. Where it
does not — a cloud session, a second machine — it injects the committed `MEMORY.md` and tells
you to read `.claude/memory/<name>.md` for an entry that looks relevant. It deliberately does
**not** copy files into the live corpus: the project key is derived from the working
directory, its form on a Linux host is unverified, and a wrong key writes where nothing reads.

Memories written in a session reach the committed copy **automatically**, as of 2026-09-06:
the `sync-memory.sh` Stop hook from `personal@progdroid` copies live → committed after every
turn. It copies only — it never stages and never commits. `scripts/flush-memory.sh` does the
same thing on demand and is still worth keeping: it works when the plugin is not loaded (a
fresh clone, or before `/reload-plugins`), and it prints a diff summary first.

That hook did nothing here at all until 2026-09-06, which is worth knowing because the
failure was completely silent. Its shared `memory_key()` derived the project key from
`git rev-parse --show-toplevel`, which on Git Bash returns `G:/pythonDev/news-brief` — a
Windows path — while the matcher only understood the MSYS `/g/...` form. The key came back
as `G:-pythonDev-news-brief`, no such directory could exist, and every call hit a
`[ -d ] || return 0` guard without a word. Fixed in `personal@0.11.1`. The cloud path was
never affected: a Linux root parses correctly, which is exactly why it went unnoticed.

**The automatic copy makes the next part more important, not less: this repository is PUBLIC.**
Every memory written here is a publishing candidate, and it now lands in the working tree
without you asking for it. The 2026-09-04 review of 21 files found no credentials, but did
find an operational map of the deploy host and a behavioural profile of the author — material
that wants a human glance, not a hook. **Review `git diff -- .claude/memory/` before
committing**, and see `live-state-on-deploy-host.md`.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.



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
# --tmpfs: `--rm` does NOT remove anonymous volumes; 57 abandoned ones (21GB)
# filled the disk on 2026-09-10. postgres:18 moved its data dir, so the mount
# is /var/lib/postgresql (NOT .../data — the image exits 1 on that path).
# MSYS_NO_PATHCONV=1: Git Bash otherwise rewrites the mount path to C:/Program Files/Git/...
MSYS_NO_PATHCONV=1 docker run --rm -d -p 5432:5432 --tmpfs /var/lib/postgresql \
  -e POSTGRES_PASSWORD=newsbrief -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
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
