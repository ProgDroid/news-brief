---
name: newsbrief-commit-to-main
description: "news-brief is a solo repo — commit directly to main, don't branch first"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
  modified: 2026-08-16T11:07:25.280Z
---

For the news-brief repo, commit and push directly to `main` when asked. Don't create a feature branch first.

**Why:** It's a solo project — the entire history is direct-to-main commits, and on 2026-05-31 the user said "commit this and push" while on `main` with no objection to the target. The global default ("if on the default branch, branch first") is overridden here.

**How to apply:** Still only commit/push when explicitly asked. Use conventional-commit messages. Keep the Co-Authored-By trailer. No PR/branch dance unless the user requests one.

**Project memory lives IN the repo at `.claude/memory/` and is committed** — this is
deliberate (user, 2026-08-16): repo-local memory is what cloud sessions get from the
clone, since user-scope config is not part of it. So memory work is not finished
until it is committed and pushed, and `chore(memory): …` commits are an established
part of this repo's history. When a session's auto-memory path points at
`~/.claude/projects/<slug>/memory/`, write there AND sync into `.claude/memory/`.

**The repo is PUBLIC as of 2026-08-16** (`ProgDroid/news-brief`, confirmed via
`gh repo view`), which means committed memory is public too. The user asked for a
sensitive-content check before a push that day, so treat that as standing: before
pushing, verify no credentials, no live book/position JSON, and no account
identifiers enter the tree. Live state is gitignored (`from-server/`, see
[[live-state-on-deploy-host]]); a scan of all 55 tracked memory files on 2026-08-16
found no secret values.
