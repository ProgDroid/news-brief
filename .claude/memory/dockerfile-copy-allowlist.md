---
name: dockerfile-copy-allowlist
description: Dockerfile COPYs an explicit file allowlist; three places must agree (COPY + workflow paths: + ruff file lists) and since 2026-09-02 tests/test_packaging.py ENFORCES all three from the import graph
metadata:
  node_type: memory
  type: project
  originSessionId: 1cd1f6f9-33e0-4195-ac7f-5611a851d9df
  modified: 2026-09-02T20:40:27.412Z
---

The Dockerfile copies first-party modules by an explicit allowlist (`COPY common.py config.py trading.py ... .`), NOT the whole tree. A module absent from it does not exist inside the image, however present it is in the repo.

**Why nothing catches it:** local `pytest` and CI both run against a FULL CHECKOUT, where the missing module is present by definition. The COPY line is the only statement anywhere in the repo that some files are excluded. `docker-compose.yml` pulls `ghcr.io/progdroid/news-brief:latest` with no source mount and sets `restart: "no"` — so a container dying on import stays dead QUIETLY rather than crash-looping, and the first symptom is a brief that never arrives.

**Three places must agree:** (1) the Dockerfile `COPY` line, (2) `paths:` in `.github/workflows/docker-publish.yml` (else a commit touching only that file publishes NO image — the fix looks committed while production runs old code), and (3) the workflow's Lint step, whose `ruff check` / `ruff format --check` lines name files EXPLICITLY, so a new top-level module is silently never CI-linted. `tests`/`enrichment`/`scripts` are directory args, so only new TOP-LEVEL modules need the ruff edit.

**THIS IS NOW ENFORCED — `tests/test_packaging.py` (2026-09-02, `51e72de`).** Three tests derive the answer from the AST import graph rather than restating a list, so a new module is covered without anyone remembering: every module the copied set imports is itself copied; every copied module appears in each ruff invocation; every copied module is in `paths:`. Adding a module and forgetting the workflow now FAILS A TEST instead of a deploy. Don't hand-maintain a parallel list — extend those tests.

**How it kept happening anyway.** 2026-06-15 `validation.py` missing from COPY. 2026-06-24 `brief_memory.py` (all three updated, `3e6b4fb`). 2026-08-16 `validation.py` found in `paths:` but in NEITHER ruff line — never CI-linted. **2026-09-02 `config.py` missing from ALL THREE**, imported at module level by `supervisor.py:25` and `brief.py:40`, so every image published after the config-to-rows work (`1b5ba23`, `3797ecf`) could not start. Four instances of one rule that was written down and re-read each time — the lesson is that a documented rule requiring a human step is not a control, and the fix was to move the remembering into a test. See [[fix-the-tooling-dont-route-around-it]].

**Verify in the artifact, not statically.** The static test goes green the moment the COPY line is edited; that shares an assumption with grep and with reconstructing the file set. Build the image and run a real mode through it: `docker build -t nb-probe:local .` then `docker run --rm -e ANTHROPIC_API_KEY=x -e TELEGRAM_BOT_TOKEN=x -e TELEGRAM_CHAT_ID=1 -e DATABASE_URL=... nb-probe:local pgdiag`. **`ENTRYPOINT ["python", "brief.py"]`** — pass the MODE only; `python brief.py pgdiag` doubles the argv and silently prints the usage banner instead. A non-zero exit from that probe is often the harness (env-var guards fire before anything interesting), so read the message, never just the code.

The image also publishes a `type=sha` tag — pull by short-sha, not `:latest`, when you must be certain the server isn't on a stale cached image.
