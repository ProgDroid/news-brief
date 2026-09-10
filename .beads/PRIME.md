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
- **A close that retires a verification action must name the observation that
  measured it.** Ask: *which observation would differ if this were false?* If
  there isn't one, the status is `deferred`, not `closed`. Measured cost of
  skipping it: the "$2 round trip" was retired 2026-08-16 on a line nothing had
  measured. The exit path had never once succeeded — `LIVE OPEN` 7,
  `not on venue` 424, `LIVE CLOSE` **0** — and 25 days passed before anyone
  counted. A note that closes a verification removes the thing that would have
  corrected it, which is worse than a merely wrong note.
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
- `-l/--labels` (comma-separated). **`bleeding` is reserved** for anything whose
  cost ACCRUES while the issue sits open — money, data loss, silent corruption.
  Priority encodes importance; it does not encode a running meter, which is how
  news-brief-5qb ("a live position stuck unsellable is invisible outside an
  hourly log line") sat at P2 while 424 failed closes accumulated. Check
  `bd list --label bleeding` before picking work off `bd ready`.

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
