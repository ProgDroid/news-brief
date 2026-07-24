---
name: newsbrief-commit-to-main
description: "news-brief is a solo repo — commit directly to main, don't branch first"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
---

For the news-brief repo, commit and push directly to `main` when asked. Don't create a feature branch first.

**Why:** It's a solo project — the entire history is direct-to-main commits, and on 2026-05-31 the user said "commit this and push" while on `main` with no objection to the target. The global default ("if on the default branch, branch first") is overridden here.

**How to apply:** Still only commit/push when explicitly asked. Use conventional-commit messages. Keep the Co-Authored-By trailer. No PR/branch dance unless the user requests one.
