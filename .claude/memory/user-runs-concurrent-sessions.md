---
name: user-runs-concurrent-sessions
description: "The user runs multiple Claude sessions against this repo at once — unexplained working-tree changes are usually his, not cruft. Flag, never revert."
metadata: 
  node_type: memory
  type: user
  originSessionId: becd7766-6453-4c8f-ad17-340ddb7b9c76
  modified: 2026-08-31T13:24:51.688Z
---

The user works this repo from **more than one Claude session at a time**. Files can appear
modified mid-session that this session never touched.

Confirmed 2026-08-31: `brief.py` and `.env.example` showed as modified partway through a session
that had only ever touched `brief_memory.py`, the scorer and tests. His words: *"don't revert
those, I've made them from another session and they're important."*

**Why:** the default reading of an unexplained diff — leftover cruft, a bad edit, something to
clean up — is wrong here and destructive if acted on. It is in-flight work whose other half
lives in a session this one cannot see.

**How to apply:**
- `git status` at the START of work, and again before committing, so you can tell what you
  changed from what appeared.
- **Never revert, stash, or check out over a foreign change.** Read the diff, say what it is,
  and ask.
- **Commit with explicit paths** (`git add <file> ...`), never `git add -A` or `git commit -a`,
  or you will sweep his in-flight work into your commit.
- **Explicit paths are necessary but NOT sufficient — a single named file can itself carry
  another session's uncommitted work.** Measured 2026-09-02: staging exactly one file
  (`ai/learnings/probe-failures.md`) by explicit path swept in two sections written earlier the
  same day by two *other* sessions, because the file was already dirty before this session
  appended to it. Nothing was lost, but the commit message described a quarter of its own diff.
  The missing step is per-file: check whether a file is dirty **before you edit it**, not only
  after. `git status` on the whole repo at the start does not help if you read it after your own
  write, which is exactly the mistake made here.
- **The `dotfiles` / memory repo is the MOST contended surface, not the least.** Every session on
  every project writes memories into it, so `ai/learnings/*` and `ai/projects/*/memory/*` are far
  likelier to hold foreign edits than any single project file. Apply all of the above there too —
  and expect ~20 unrelated modified files to be the normal state, so "the tree is dirty" carries
  no signal and only a per-file check does.
- A foreign change may be **half a feature**. The 2026-08-31 one read two env vars that no
  compose file declared, so it would have deployed correct and completely inert — check whether
  the other half exists before assuming it is finished. See [[env-var-needs-compose-passthrough]].
- Related: [[newsbrief-commit-to-main]], [[live-state-on-deploy-host]].
