# Beads Workflow Context — news-brief

Overrides `bd prime`'s default, which stated this repo's memory policy backwards
and ran ~5.1k characters. `bd prime --export` dumps the default to diff against.

# 🚨 SESSION CLOSE PROTOCOL 🚨

Before saying "done" or "complete":

```
[ ] bd close <id1> <id2> ...
```

## Core rules

- **Task tracking is bd, always.** No TodoWrite, no TaskCreate, no markdown TODO
  lists. File the issue before writing the code.
- **bd owns WORK STATE ONLY** — issues, dependencies, what is ready next.
- **Memory is FILES, not `bd remember`.** Learnings go to
  `~/.claude/projects/<key>/memory/`; the `sync-memory.sh` Stop hook copies them
  into `.claude/memory/` for git. The bd migration was investigated and **dropped**:
  cross-machine sync needs a pinned `dolt` binary on every machine plus
  `bd bootstrap` per clone. Do not re-propose it. See `CLAUDE.md`.
- **Git authority: none.** Stealth mode, no git operations from bd. Commits and
  pushes are the developer's, and this repo is PUBLIC — review
  `git diff -- .claude/memory/` before committing.

## Commands

**Find work**
`bd ready` · `bd blocked` · `bd list --status=open|in_progress` ·
`bd show <id>` · `bd search <query>` · `bd stats`

**Create and update**
`bd create --title="…" --description="why this exists and what to do" --type=task|bug|feature --priority=2`
- Priority is `0-4` / `P0-P4` (0 = critical, 2 = medium, 4 = backlog). Never
  "high"/"medium"/"low".
- `--parent=<id>` for a child of an epic; inherits parent labels.
- `--acceptance=` / `--design=` / `--notes=` for the structured fields;
  `--validate` checks they are present.

`bd update <id> --claim` · `bd update <id> --title/--description/--notes/--design`
`bd close <id1> <id2> …` · `bd close <id> --reason="…"` · `bd close <id> --suggest-next`

**Dependencies**
`bd dep add <issue> <depends-on>` — the first argument is blocked BY the second.

**Hygiene**
`bd defer <id> --until="date"` · `bd supersede <id> --with=<new-id>` ·
`bd stale` · `bd orphans` · `bd lint` · `bd doctor` · `bd preflight`

## Traps

- **Never `bd edit`** — it opens `$EDITOR` and blocks the agent indefinitely.
- Closing several issues in one `bd close` call is cheaper than one call each.
- Architecture: issues live in a local Dolt DB, sync rides `refs/dolt/data` on the
  git remote, and `.beads/issues.jsonl` is a passive export — not the source of truth.
