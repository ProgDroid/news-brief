# modal_apps

The Modal embedding service. Not part of the Docker image; deployed by hand.

- `embed_core.py`: request validation, prompts, Matryoshka cut, reply shape. Standard
  library only and tested (`tests/test_embed_core.py`).
- `embed_service.py`: the Modal app (GPU class, CPU memory snapshot, proxy-auth
  endpoint). Not imported by tests: CI has no `modal`. Its checks are `ruff check` and
  an AST parse; it has never been run by the author of this file.

## Deploy (one app per candidate; belongs in the Rollout section)

```bash
EMBED_MODEL=Qwen/Qwen3-Embedding-4B  EMBED_GPU=T4 modal deploy modal_apps/embed_service.py
EMBED_MODEL=Qwen/Qwen3-Embedding-8B  EMBED_GPU=L4 modal deploy modal_apps/embed_service.py
EMBED_MODEL=Octen/Octen-Embedding-8B EMBED_GPU=L4 modal deploy modal_apps/embed_service.py
```

The app is named `newsbrief-embed-<model slug>`. Weights download into the Volume
`newsbrief-embed-models` on first start and are reused afterwards. GPU choice sets the
dtype (`DTYPE_BY_GPU`): float16 on a T4, bfloat16 on L4 and A10.

GPU memory per candidate: not yet measured. Record it here after the first deploy of
each (a 4B model in 16-bit needs about 8 GB of weights; an 8B model about 16 GB, which
does not fit a T4).

## Things that must stay in step

- `scaledown_window` (10 s in `embed_service.py`): if you change it, set
  `EMBEDDING_MODAL_IDLE_SECONDS` to the same value, or the cost record under- or
  over-charges the idle tail of every cold start.
- `PROMPT_VERSION` in `embed_core.py` and `EMBED_PROMPT_VERSION` in `embeddings.py`:
  change both when `PROMPTS` changes (a test fails if they differ). That retires
  cached query vectors and forces a recalibration.
- `MAX_INPUTS` equals `embeddings.MODAL_MAX_INPUTS` (a test pins it).

## Secrets and URLs

The endpoint uses Modal proxy auth: clients send `Modal-Key` and `Modal-Secret`
(`MODAL_PROXY_KEY`, `MODAL_PROXY_SECRET` in the environment). The endpoint URLs go in
the `EMBEDDING_MODAL_URL` settings row and on the host, never in git (the repo is
public).

## Image pins

Each is the latest release at least 14 days old on 2026-11-24 (upload date from PyPI):

| package | version | uploaded |
|---|---|---|
| sentence-transformers | 6.2.0 | 2026-10-21 |
| torch | 2.15.0 | 2026-10-28 |
| transformers | 5.22.2 | 2026-11-06 |
| fastapi | 0.162.2 | 2026-11-07 |

Newer releases skipped as too young: sentence-transformers 6.3.0 (2026-11-11).
transformers is at least 4.51, which Qwen3 requires.

## Not used

GPU memory snapshots (alpha in Modal's docs on 2026-11-24). Only the CPU snapshot is on.

## What `boot_s` is

`boot_s` is the duration of the `snap=False` hook, the move of the model to CUDA. It is
not the container's billed boot time (image start and snapshot restore are not in it),
so it under-reports. Reconcile it against Modal's usage page at Rollout step 11.
