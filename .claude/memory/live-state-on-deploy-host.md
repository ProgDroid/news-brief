---
name: live-state-on-deploy-host
description: "Live mutable state (book/signals/watchlist/feedback/sources/enrichment) lives on the DEPLOYMENT HOST volume, NOT in the dev repo — local runs against real data need the JSONs copied over"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4356315d-665d-43bb-a1c0-dba9009690e0
---

The repo on this dev machine is pure code. ALL mutable runtime state lives on the deployment host, not here.

**Why:** `common.py` sets `DATA_DIR = Path(os.environ.get("NEWSBRIEF_DATA_DIR", "/app/logs"))` and `SIGNALS_DIR = DATA_DIR/"signals"`; `trading.py` keeps `book.json`/`watchlist.json` under `PAPER_DIR`; `brief.py` has `feedback.json`, plus `sources.json` and `enrichment/enrichment-<date>.json` — all under `DATA_DIR`. In prod that's the container's `/app/logs`, bind-mounted from `${APPDATA_DIR}/news-brief` on the deploy host (see `docker-compose.yml`). The dev box has **no** `APPDATA_DIR` in `.env`, no local `appdata/`, and no running container — so none of that state exists locally. This is the same deliberate code/state split that lets `/addsource` persist without a redeploy ([[telegram-source-management-daemon]]).

**How to apply:** To run/validate anything against REAL state on this machine (e.g. the enrichment universe from live book/signals), you must get the JSONs first: `docker cp`/`scp` `book.json`, `watchlist.json`, the latest `signals/signals-*.json`, `feedback.json` off the deploy host into a local dir, then point `NEWSBRIEF_DATA_DIR` at it (or feed them to the relevant function). Don't assume the repo has them — it never will. The fastest path is often just to ask the user to paste the files (done 2026-06-20 for the Bigdata Step-2 universe). See [[brief-local-run]].

**Best technique — run a read-only probe IN-PLACE on the host (used heavily 2026-06-25 to debug PolyGram + the signals timeout):** instead of exfiltrating JSONs, write a small probe `.py` that imports the project modules, mount it into the deployed image, and run it on the host so it sees the real volume + env (creds, network) live. The user runs it; you author the probe and read the printed output. Exact invocation (the compose stack needs BOTH env-files and the `--file`):
`docker compose --file ./news-brief.yml --env-file ../global.env --env-file ./news-brief.env run --rm -v "$PWD/probe.py:/app/probe.py" --entrypoint python newsbrief-collect /app/probe.py`
`--entrypoint python` OVERRIDES the mode-dispatch entrypoint so it runs the probe, NOT a collect — no state mutation. To exercise UNRELEASED code before the CI image rebuilds, `git pull` on the host then ALSO mount the changed module: add `-v "$PWD/trading.py:/app/trading.py"`. CAUTION: the host **re-runs the whole pipeline on every image pull** (a deploy = a fresh collect), so a redeploy regenerates/overwrites today's `signals-*.json` and re-opens paper positions — a broken post-gen call corrupts state on every pull, not just at the 6am cron.
