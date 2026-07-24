---
name: dockerfile-copy-allowlist
description: Dockerfile COPYs an explicit file allowlist; a new top-level module needs THREE updates (Dockerfile COPY + workflow paths: + workflow ruff file lists) or it ModuleNotFounds at runtime / escapes CI lint
metadata: 
  node_type: memory
  type: project
  originSessionId: 1cd1f6f9-33e0-4195-ac7f-5611a851d9df
---

The Dockerfile copies first-party modules by an explicit allowlist (`COPY common.py trading.py validation.py brief.py .`), NOT the whole tree. brief.py's first-party imports are: `common`, `trading`, `validation` (the latter two also import only `common`).

**Why:** CI's `test` job runs `pytest` against the full checkout, so a module missing from the image still passes tests — the gap only surfaces as `ModuleNotFoundError` when the container runs on the server. Same failure class as [[newsbrief-deferred-findings]]: a step using the full repo masks a step using a curated subset. Hit 2026-06-15 — validation.py (added in the [[multi-asset-trading-build]]) was never added to the COPY line.

**How to apply:** When adding a new first-party `.py` module, update THREE places: (1) the Dockerfile `COPY` line, (2) the `paths:` filter in `.github/workflows/docker-publish.yml` (else a change touching only that file won't even rebuild the image), and (3) the workflow's **Lint** step — its `ruff check …` / `ruff format --check …` lines name files EXPLICITLY (`brief.py common.py trading.py enrichment tests`), so a new top-level module is silently NOT CI-linted unless added there too (confirmed 2026-06-24 adding `brief_memory.py` — all three updated in commit 3e6b4fb; current COPY line is now `COPY common.py trading.py validation.py brief.py brief_memory.py .`). Note `tests`/`enrichment` are directory args so new files inside them are auto-covered — only new TOP-LEVEL modules need the ruff-list edit. The image publishes a `type=sha` tag too — pull by short-sha, not `:latest`, when you must be certain the server isn't on a stale cached image.
