---
name: memory-corpus-does-not-travel
description: "RESOLVED 2026-09-04 — news-brief had TWO memory corpora and only one was live; the committed copy is now current and a hydrate hook injects it where the live one is absent. The repo is PUBLIC, so flushing is manual and reviewed."
metadata: 
  node_type: memory
  type: project
  originSessionId: 09d3ac98-bf2b-4ca7-8fdc-394dccdc1bc1
  modified: 2026-09-04T15:47:45.119Z
---

**RESOLVED 2026-09-04 (`news-brief-iad`), option A.** He chose "make it true" over "make it
honest" on one criterion: *"memory should not be local only as I need it in remote sessions."*

## What was wrong

Two copies existed and only one was live. `<repo>/.claude/memory/` was an orphaned 2026-08-29
snapshot — 60 files against 68 live, and 13 of the shared ones drifted. **Nothing hydrated it**:
`.claude/settings.json` held exactly one SessionStart hook, `bd prime --stealth --hook-json`, and
no `hydrate-memory` hook existed in either scope.

Meanwhile `CLAUDE.md` justified rejecting `bd remember` on the grounds that the corpus was
"hydrated by the hydrate-memory SessionStart hook" and "already travels with the repo". Every
clause was false. Textbook **`metadata-is-not-state`**: a document that GOVERNS a repo still only
states intent. `.claude/settings.json` is the discriminating field; read it.

## What exists now

- `.claude/hooks/hydrate-memory.sh`, registered in the repo's `.claude/settings.json`. Where the
  live corpus is present it prints **nothing** (the index is already loaded; a second copy is
  noise). Where it is absent it injects the committed `MEMORY.md` and points at
  `.claude/memory/<name>.md`. Same shape as `aegypt-wiki-context.sh`.
- `scripts/flush-memory.sh` — copies live → committed, then **stops**. No `git add`, no commit.
- `CLAUDE.md` has a Memory section describing the above, opening with the correction.
- Corpora verified byte-identical at 68 files (`diff -rq`, exit 0).

## The two design decisions worth not re-litigating

**The hook INJECTS CONTEXT; it does not copy files into the live corpus.** That was the first
design and it is wrong twice. The project key is derived from cwd (`/g/a/b` -> `G--a-b`), and
**all 26 keys on this machine are Windows drive paths — the Linux form is UNVERIFIED**. A wrong
key writes to a directory nothing reads, silently, on the very platform the feature exists to
serve. Copying would also overwrite a live corpus that is normally AHEAD, destroying memories to
fix staleness. Measured: `/home/user/news-brief` and `/workspace/news-brief` derive no key at
all, so detection fails to "absent" and the hook injects — **the unverifiable half fails in the
safe direction**, which is why the presence check is allowed to use a derivation I cannot fully
verify.

**The flush is MANUAL because the repo is PUBLIC.** An automatic flush makes every memory write
an unreviewed publication. Reviewing all 21 changed files found no credentials, but did find an
operational map of the deploy host (`live-state-on-deploy-host.md` carries the exact
`docker compose --file ... --env-file ...` invocation) and a behavioural profile of him with
verbatim quotes (`user-working-style.md`). He approved publishing all 21.

## The fact that decided the review, and generalises

**13 of the 21 were updates to files ALREADY in public git history**, so withholding an update
would not have unpublished anything — on a public repo the history is public too. Only the 8 new
files were a real decision. Before agonising over publishing a change, check whether the thing
was already published: the decision may have been made months ago and be unreachable now.

## Standing consequence

Every memory written in this repo is a publishing candidate. Write to the projects path as
before; run `scripts/flush-memory.sh` and read `git diff -- .claude/memory/` before committing.
See [[live-state-on-deploy-host]] and [[user-working-style]] (he audits what gets published, and
asked "anything that can't be public?" unprompted the last time this came up).

**Still open: `news-brief-6qi`** — compact `MEMORY.md`, 22.1KB against a 24.4KB read limit. His
call 2026-09-04 that it gets its own session and must not ride along with feature work. It is now
the more urgent of the two, because the hook injects that same file into every cloud session.
