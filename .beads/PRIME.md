# Beads Workflow Context — news-brief

Overrides `bd prime`'s default, which stated this repo's memory policy backwards
and ran ~5.1k characters. `bd prime --export` dumps the default to diff against.

# 🚨 SESSION CLOSE PROTOCOL 🚨

Before saying "done" or "complete":

```
[ ] bd close <id1> <id2> ...
```

## Core rules

- **Durable task tracking is bd, always.** File the issue before writing the
  code. Anything that must survive the session is a bead, never a markdown TODO.
- **TodoWrite is NOT durable task tracking, and is NOT banned.** It carries
  *within-turn execution structure* — the checklist a skill tells you to work
  through. The two are different layers: bd is *what work exists*, TodoWrite is
  *how a procedure gets followed*. Banning it does not move that state into bd,
  it deletes it, and it silently disables `systematic-debugging`,
  `test-driven-development` and `verification-before-completion` — whose absence
  showed up here on 2026-09-10 as `the-filter-presupposed-the-answer` (a probe
  that selected DB fields by name pattern for a month; the answer was
  `position_key`). Use both.
- **bd owns WORK STATE ONLY** — issues, dependencies, what is ready next.
- **Memory is FILES, not `bd remember`.** Learnings go to
  `~/.claude/projects/<key>/memory/`; the `sync-memory.sh` Stop hook copies them
  into `.claude/memory/` for git. The bd migration was investigated and **dropped**:
  cross-machine sync needs a pinned `dolt` binary on every machine plus
  `bd bootstrap` per clone. Do not re-propose it. See `CLAUDE.md`.
- **Git authority: none.** Stealth mode, no git operations from bd. Commits and
  pushes are the developer's, and this repo is PUBLIC — review
  `git diff -- .claude/memory/` before committing.

## Labels

- **`bleeding` is reserved** for anything whose cost ACCRUES while the issue sits
  open — money, data loss, silent corruption. Priority encodes importance; it does
  not encode a running meter, which is how news-brief-5qb ("a live position stuck
  unsellable is invisible outside an hourly log line") sat at P2 while 424 failed
  closes accumulated. Check `bd list --label bleeding` before picking work off
  `bd ready`.
- **How much of the user a bead needs: exactly one of these on every bead you file.**
  - **`quick`**: doable in a break; small, clear, needs nothing from the user
    beyond a yes.
  - **`handoff`**: a short conversation up front (a decision, a brainstorm, a plan
    to approve), then it runs without the user for hours.
  - **`hands-on`**: needs the user throughout (judging output, host changes,
    anything only they can check).

  Judge it from the work, never from how long the code would take. **No time
  estimates** (`--estimate`): the user found them misleading (2026-11-09).
- **Dates, set by whoever files the bead:** `--due=<date>` when the work has a
  deadline; `--defer=<date>` at create, or `bd defer <id> --until=<date>`, when it
  waits on time. switchboard's board lists passed defers and near deadlines, so a
  wait recorded only in prose is invisible to it.

## Commands

**Find work**
`bd ready` · `bd blocked` · `bd list --status=open|in_progress` ·
`bd show <id>` · `bd search <query>` · `bd stats`

**Create and update**
`bd create --title="…" --description="why this exists and what to do" --type=task|bug|feature --priority=2 -l quick|handoff|hands-on`
- Priority is `0-4` / `P0-P4` (0 = critical, 2 = medium, 4 = backlog). Never
  "high"/"medium"/"low".
- `--parent=<id>` for a child of an epic; inherits parent labels.
- `--acceptance=` / `--design=` / `--notes=` for the structured fields;
  `--validate` checks they are present.
- `-l/--labels` comma-separated; see Labels above.

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
