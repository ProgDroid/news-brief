# Storylines plan 1: groups, embeddings and retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every captured item gets an embedding and a place in an event group, and a hybrid search over groups with an `as_of` cut-off exists for plan 2's editor and replay to call.

**Architecture:** Postgres gains pgvector (image swap, `bqa.7`) and migration `0020`. `embeddings.py` is one interface over two providers: the operator's Modal deployment (`modal_apps/`, an open 4B–8B model on a GPU) and OpenAI, chosen by `EMBEDDING_PROVIDER`. Vectors are stored per **space** (`provider:model@dim`), so spaces coexist and only the active one is read. `grouping.py` runs a new 30-minute `group` job. It embeds new items, then assigns them to groups strictly in capture order, using thresholds calibrated per space and stored in a table, never in knobs. `retrieval.py` is the hybrid search (vector plus trigram, rank-fused) that plan 2 calls. All SQL lives in `group_store.py`. Two scripts do the bulk work: calibration (model pick and thresholds, judged by Opus) and backfill (Modal, or OpenAI's Batch API). The fetch probe is re-run with a real extractor.

**Tech Stack:** Python 3.12, psycopg 3, Postgres 18 + pgvector 0.8.7 + pg_trgm, `requests`, Modal (sentence-transformers on a GPU), the OpenAI embeddings and Batch APIs, trafilatura.

**Spec:** `docs/superpowers/specs/2026-11-24-storylines-editor-design.md` (approved 2026-11-24; cost pass §21). Plan 1 is §23 item 1. Read §6.1, §6.2, §15.1, §18, §19, §21.2 and §22.

## Global Constraints

- **The repo is PUBLIC.** Tests and fixtures use invented text only (Widget, Gizmo, TESTCO). No script prints a storyline name, a description, thesis text, a URL or article text: counts, metrics and outlet names only. Calibration labels, snapshots and Modal URLs go under `common.DATA_DIR`, never into git.
- **The gate is three commands over the whole tree,** with a database configured, on the **pgvector image** (Task 1 changes the documented command):
  ```bash
  ruff check . && ruff format --check . && pytest -q
  ```
  A DB-gated module that reports "skipped" has not run. Record the suite's pass count before Task 1 and after every task; it only goes up.
- **Postgres image:** `pgvector/pgvector:0.8.7-pg18-trixie` (pgvector 0.8.7, released 2026-10-01), **pinned by digest** in CI and compose (the tag is rebuilt in place; last pushed 2026-11-12), and named in every documented command.
- **Nothing from this plan is pushed until the host runs Postgres on that image** (Rollout step 0). Migration 0020 runs at supervisor start, and on `postgres:18-alpine` it would fail, which disables every job (`supervisor.py:463-510`). The controller commits locally and pushes only after the operator confirms step 0.
- **Knobs:** a `common.KNOBS` entry plus a compose anchor line (`- NAME=${NAME:-}`). Read as `common.X`, never `from common import X`. Resolve once per pass and thread the value through. Credentials (`OPENAI_API_KEY`; the existing `MODAL_PROXY_KEY` / `MODAL_PROXY_SECRET`) stay environment variables with a compose line, never a settings row.
- **New top-level modules** (`embeddings.py`, `group_store.py`, `grouping.py`, `retrieval.py`) go in the Dockerfile `COPY` line and in the workflow's `paths:` list; `tests/test_packaging.py` must pass. `modal_apps/` is **not** shipped in the image.
- **SQL lives only in `group_store.py`** for the 0020 tables. Nothing in it commits; callers commit. No DELETE from any 0020 table; the down migration refuses while any row exists (the `0019_storylines_down.sql` pattern: `to_regclass`, nested IF).
- **Retry budget:** a timeout, a 429 or a 5xx is `transient` and defers. A 401, 403 or 404, a missing URL or key, or any other 4xx is `config`. A reply of the wrong shape is `bad_response`. Nothing permanently retires an item: an item that cannot be embedded waits, and its age raises the alert (Task 6).
- **Never give a model raw database ids** (calibration judge): per-call opaque labels (`P1`, `Q3`), mapped back in code; an unknown label is an error, never a fall-through.
- **Tests never reach the network** (`tests/conftest.py` guard): every provider takes an injectable `post` / `get`.
- **Commits:** `git commit -F <file>` from Bash, explicit paths only, never `-A`. Reference beads inline (`Refs news-brief-bqa.7`); only the commit that finishes a bead puts its id alone on a line. Before each commit, grep the staged diff for `and False`, `if False` and `if True`.
- **Cosine:** every stored vector is L2-normalised, so similarity is `1 - (a <=> b)`.

## Review Focus

1. **A provider outage in the middle of a backlog must not reorder grouping.** An item is assigned only after every earlier item (by `created_at`, then `id`) has been, so no item is grouped against a later one. Pinned by `test_assignment_stops_at_the_first_item_without_a_vector` (Task 4).
2. **Syndicated copy.** Identical text from two outlets is one provider input and two rows, and the two items land in one group. Pinned by `test_identical_text_is_embedded_once_and_groups_together` (Task 4).
3. **A switched provider or model must not reuse another space's thresholds.** Grouping in an uncalibrated space embeds but never assigns, returns `uncalibrated` naming the space, and rings. Pinned by `test_uncalibrated_space_embeds_but_does_not_assign` (Task 4) and `test_mode_group_rings_on_uncalibrated` (Task 6).
4. **Nothing after `as_of` leaks into retrieval.** Not a member, not an outlet count, not a `last_seen`. Pinned by `test_as_of_hides_later_members_and_their_outlets` (Task 5).
5. **Google News proxy titles carry " - <Publisher>", and the body repeats the title,** joined by a no-break space. Left in, every Reuters item shares a trigram tail and its body adds nothing. The publisher name is Google's, not our outlet name ("Yonhap (English)" is not what Google appends). Pinned by `test_embed_text_strips_the_google_suffix_and_a_repeated_body` (Task 2).
6. **One item the provider can never embed must not stall everything behind it.** An empty text, or an input the provider rejects on its own, gets a skip row, and assignment passes over it. Pinned by `test_an_unembeddable_item_is_skipped_and_the_queue_moves` (Task 4).

---

## File structure

| File | Responsibility |
|---|---|
| `migrations/0020_groups_up.sql`, `_down.sql` | a guard, then the `vector` extension; `item_embeddings`, `item_embed_skips`, `query_embeddings`, `embedding_calls`, `embedding_calibrations`, `item_groups`, `item_group_members`; spend stage `calibrate` |
| `embeddings.py` | embed text and hashing, space ids, vector literals, `ModalEmbedder`, `OpenAIEmbedder`, cost records, `active_embedder()` |
| `group_store.py` | all 0020 SQL: vectors, calls, calibrations, groups, retrieval queries, month-to-date cost |
| `grouping.py` | `decide_group`, `embed_pending`, `assign_pending`, `group_pass` |
| `retrieval.py` | `search_groups` (hybrid, `as_of`, query-vector cache, trigram fallback) |
| `brief.py` | `mode_group`, its alert, the weekly embeddings line |
| `scheduler.py`, `common.py` | the `group` schedule; knobs; two Claude prices |
| `modal_apps/embed_core.py` | pure request/response logic of the Modal service (tested) |
| `modal_apps/embed_service.py` | the Modal app: GPU class, snapshot, proxy-auth endpoint |
| `scripts/embed_backfill.py` | embed (Modal, or OpenAI Batch API) and group every item for a space |
| `scripts/embed_calibrate.py` | model comparison, thresholds, retrieval floor; writes the calibration row on `--accept` |
| `scripts/storylines_fetch_probe.py` | adds a trafilatura extractor column |
| `docker-compose.yml`, `Dockerfile`, `.github/workflows/docker-publish.yml`, `CLAUDE.md`, `requirements.txt` | image, knobs, allowlists, docs |

---

### Task 1: pgvector in the stack, and migration 0020

**Files:**
- Modify: `.github/workflows/docker-publish.yml` (the `services.postgres.image`), `docker-compose.yml` (the `postgres:` image plus a comment), `CLAUDE.md` (the `docker run` line in Build & Test), and the skip-reason strings in the 9 test files that name `postgres:18-alpine` (`grep -ln "postgres:18-alpine" tests/*.py`)
- Create: `migrations/0020_groups_up.sql`, `migrations/0020_groups_down.sql`
- Test: `tests/test_groups_schema.py`

**Interfaces:**
- Produces: the tables below, used by every later task.

The up migration, in order:
1. A guard: a `DO` block that raises `'pgvector is not installed in this Postgres image: run plan 1 Rollout step 0 (the image swap) first'` when `vector` is absent from `pg_available_extensions`. Then `CREATE EXTENSION IF NOT EXISTS vector;`.
2. `item_embeddings (item_id BIGINT REFERENCES items(id) ON DELETE RESTRICT, space TEXT NOT NULL, text_hash TEXT NOT NULL, dim INT NOT NULL CHECK (dim > 0), embedding vector NOT NULL CHECK (vector_dims(embedding) = dim), created_at, PRIMARY KEY (item_id, space))` with an index on `(space, text_hash)`.
   - **No ANN index, scoped to grouping:** a 72 h window is about 2.4k members, so an exact scan takes milliseconds. An index would also pin a dimension, and spaces of different dimensions share this table.
   - Plan 2's 30-day reads (about 24k members per query) are measured there; an index is added only if they are slow.
2a. `item_embed_skips (item_id BIGINT REFERENCES items(id), space TEXT, reason TEXT NOT NULL CHECK (length(reason) <= 300), created_at, PRIMARY KEY (item_id, space))`. An item judged unembeddable in a space: an empty text, or an input the provider rejected when sent alone. Assignment passes over it.
3. `query_embeddings (text_hash TEXT, space TEXT, dim INT, embedding vector, created_at, PRIMARY KEY (text_hash, space))`, with the same dimension check.
4. `embedding_calls (id BIGSERIAL, provider TEXT CHECK IN ('modal','openai'), space TEXT, purpose TEXT CHECK IN ('items','queries','backfill','calibration'), inputs INT, tokens INT, runtime_s REAL NULL, boot_s REAL NULL, cold_start BOOLEAN NULL, batch BOOLEAN NOT NULL DEFAULT FALSE, usd NUMERIC(12,6) NOT NULL, openai_equivalent_usd NUMERIC(12,6) NOT NULL, outcome TEXT CHECK IN ('ok','failed'), problem TEXT NULL CHECK (length(problem) <= 300), created_at)`, with an index on `(created_at)`.
5. `embedding_calibrations (id BIGSERIAL, space TEXT NOT NULL, join_sim REAL NOT NULL, pair_sim REAL NOT NULL, title_sim REAL NOT NULL, retrieval_min_sim REAL NULL, weighted_precision REAL, weighted_recall REAL, pairs_labelled INT, prompt_version INT NOT NULL, judge_model TEXT, week_ending DATE, created_at)`. The latest row per space is the one in force. Each similarity has `CHECK (x > 0 AND x < 1)`, and `pair_sim <= join_sim`. `retrieval_min_sim` stays NULL until plan 2 fits it on real document queries (Task 8).
6. `item_groups (id BIGSERIAL, space TEXT NOT NULL, opened_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, title TEXT NOT NULL, outlet_count INT NOT NULL CHECK (outlet_count >= 1), has_primary BOOLEAN NOT NULL, created_at)`, with an index on `(space, opened_at)`. `opened_at` is the founding item's `created_at`.
7. `item_group_members (item_id BIGINT REFERENCES items(id) ON DELETE RESTRICT, space TEXT, group_id BIGINT REFERENCES item_groups(id), joined_at TIMESTAMPTZ NOT NULL, similarity REAL NULL, title_similarity REAL NULL, created_at, PRIMARY KEY (item_id, space))`, with indexes on `(space, joined_at)` and `(group_id)`. `joined_at` is the item's own `created_at` (capture time), never wall clock. That is what makes `as_of` cuts consistent. The founder's similarities are NULL.
8. Widen `comprehend_spend_stage_check` to add `'calibrate'`.

The down migration refuses while any of the seven tables has a row (nested IF, `to_regclass`, and a `calibrate` spend-row check as 0019 does), then drops them in reverse, restores the 0019 stage check, and runs `DROP EXTENSION IF EXISTS vector`.

In compose, the comment above `image:` says three things: the swap is from `postgres:18-alpine` (musl) to a Debian (glibc) image, so the operator runs `REINDEX DATABASE` once after it (Rollout step 3); the data volume line does not change; and `backup.py`'s major-version check still holds, since both are 18.

- [ ] **Step 1: Swap the image.**
  - Resolve the digest: `docker pull pgvector/pgvector:0.8.7-pg18-trixie`, then `docker inspect --format '{{index .RepoDigests 0}}'`. Pin it in the workflow and compose.
  - Update the `CLAUDE.md` command, the 9 test skip strings (`grep -ln "postgres:18-alpine" tests/*.py`) and the operational docs that name the old image: `docs/2026-09-22-host-runbook-comprehension-restart.md`, and the live memory files `brief-local-run.md` and `newsbrief-comprehension-pipeline.md`. Do **not** edit applied migrations (the 0012 comment) or historical plans and reviews.
  - Start the documented container locally (`MSYS_NO_PATHCONV=1 docker run --rm -d -p 5432:5432 --tmpfs /var/lib/postgresql -e … <the pinned image>`) and run the full gate. It must pass at the count you recorded, with zero DB modules skipped.
  - That proves only that a **fresh** pgvector cluster passes the suite. The production cluster's own risk (text indexes built under musl) is checked in Rollout step 0.
- [ ] **Step 2: Write the failing tests** in `tests/test_groups_schema.py` (DB-gated; the `conn`/`store` fixtures as in `tests/test_storyline_store.py`):
  - `test_vector_extension_is_installed`: `SELECT extname FROM pg_extension WHERE extname = 'vector'` returns one row, and `SELECT 1 - ('[1,0]'::vector <=> '[0,1]'::vector)` returns `0.0`.
  - `test_embedding_dimension_must_match`: inserting `dim = 3` with a 2-element vector raises `psycopg.errors.CheckViolation`.
  - `test_two_spaces_of_different_dimensions_coexist`: the same `item_id` in spaces `a@2` and `b@3` both insert.
  - `test_calibration_rejects_pair_above_join`: `pair_sim = 0.9, join_sim = 0.8` raises a CheckViolation.
  - `test_down_refuses_while_rows_exist`: insert one `embedding_calls` row; running 0020 down raises, and the tables still exist.
  - `test_guard_message_names_the_image_swap`: read the up file and assert the guard's message contains `Rollout step 0`. The guard cannot be exercised on a pgvector image; this pins its text.
  - `test_down_then_up_round_trips_on_an_empty_schema`: down then up succeeds; afterwards `vector` is installed and `comprehend_spend` accepts `stage = 'calibrate'`.
- [ ] **Step 3: Run them:** `pytest tests/test_groups_schema.py -v`. Expected: FAIL (no such migration).
- [ ] **Step 4: Write both migrations.**
- [ ] **Step 5: Run the gate.** Count = recorded + 7.
- [ ] **Step 6: Commit:** `feat(storylines): pgvector and the groups schema, migration 0020 (Refs news-brief-bqa.7)`.

---

### Task 2: `embeddings.py`, one interface over two providers

**Files:**
- Create: `embeddings.py`, `tests/test_embeddings.py`
- Modify: `common.py` (knobs), `docker-compose.yml` (anchor lines, plus `OPENAI_API_KEY=${OPENAI_API_KEY:-}` with a comment saying it is the one privacy exception, spec §19)

**Interfaces:**
- Produces:
  - `EMBED_TEXT_MAX_CHARS = 1000` (about 256 tokens), `MODAL_MAX_INPUTS = 128`, `OPENAI_MAX_INPUTS = 512`, `OPENAI_MAX_TOKENS_PER_REQUEST = 250_000` (estimated at chars ÷ 4).
  - `OPENAI_PRICES_PER_MTOK = {"text-embedding-3-large": 0.13, "text-embedding-3-small": 0.02}`; `OPENAI_EQUIVALENT_MODEL = "text-embedding-3-large"`; `OPENAI_BATCH_FACTOR = 0.5`.
  - `embed_text(title: str, body: str | None, url: str) -> str`.
    - First it normalises whitespace in both fields (U+00A0 and other Unicode spaces become one ASCII space, and the ends are stripped).
    - When the URL's host is `news.google.com`, it strips the title's last `" - X"` segment, since X is Google's publisher name, not our outlet name. It then drops the body when that body is the stripped title optionally followed by one more short segment (the publisher).
    - It also drops an empty body.
    - It joins title and body with `"\n"` and cuts at `EMBED_TEXT_MAX_CHARS` on a word boundary.
    - If stripping leaves an empty title, it uses the unstripped title. It returns `""` only when the title is blank, which the caller turns into a skip row.
  - `EMBED_PROMPT_VERSION = 1`. It goes in the Modal request and must come back in the reply; it also goes into every query-cache key and every calibration row. A change to the service's prompts bumps it on both sides, which retires cached query vectors and forces a recalibration instead of silently mixing prompts.
  - `text_hash(text: str) -> str` (sha256 hex of the UTF-8 text).
  - `space_id(provider: str, model: str, dim: int) -> str` gives `f"{provider}:{model}@{dim}"`.
  - `to_pg_vector(vec: Sequence[float]) -> str` (`"[0.123456,…]"`, 6 decimals) and `from_pg_vector(text: str) -> list[float]`.
  - `@dataclass(frozen=True) class CallRecord: provider: str; space: str; inputs: int; tokens: int; runtime_s: float | None; boot_s: float | None; cold_start: bool | None; batch: bool; usd: float; openai_equivalent_usd: float; outcome: str; problem: str | None`.
  - `class EmbedError(Exception)` with `.kind` (`"transient" | "config" | "input" | "bad_response"`) and `.calls: list[CallRecord]`, which holds every call made before the failure, the failed one included.
    - `input` is a 400, 413 or 422: the request's content was refused, not the service.
    - `config` is a 401, 403 or 404, or a missing URL or key.
    - `transient` is a timeout, a connection error, a 429 or a 5xx.
  - `class Embedder(Protocol)`: `provider: str`, `space: str`, `embed(texts: list[str], *, kind: Literal["document", "query"]) -> tuple[list[list[float]], list[CallRecord]]`. The vectors are in input order and L2-normalised; the method raises `EmbedError`.
  - `ModalEmbedder(url, model, dim, *, key, secret, gpu_rate, idle_s, timeout, post=requests.post)`. It chunks at `MODAL_MAX_INPUTS` and sends `{"texts", "dim", "kind", "model", "prompt_version"}` with headers `Modal-Key` and `Modal-Secret`. Query text is prepared **on the service**: `embeddings.py` never imports `modal_apps`, which is not in the image. Each call's `usd = (runtime_s + (boot_s + idle_s if cold_start else 0)) × gpu_rate`: a cold start implies one billed idle tail when that container ends. `openai_equivalent_usd = tokens × OPENAI_PRICES_PER_MTOK[OPENAI_EQUIVALENT_MODEL] / 1e6`. A reply whose `model` or `dim` differs from what was asked is `bad_response`.
  - `OpenAIEmbedder(api_key, model, dim, *, timeout, post=requests.post)`. It POSTs `https://api.openai.com/v1/embeddings` with `{"model", "input", "dimensions": dim, "encoding_format": "float"}`, chunking at `OPENAI_MAX_INPUTS` or the token estimate, whichever comes first. It reorders `data` by `index`, re-normalises, and prices from `usage.prompt_tokens`; `openai_equivalent_usd = usd`. `kind` is ignored, since OpenAI has no query instruction.
  - `active_embedder() -> tuple[Embedder | None, str | None]` reads the knobs once and gives `(embedder, None)` or `(None, problem)`, for example `"EMBEDDING_MODAL_URL is empty"` or `"OPENAI_API_KEY is not set"`.
- Knobs (with compose lines): `EMBEDDING_PROVIDER` (str, `"modal"`), `EMBEDDING_MODAL_MODEL` (str, `"Qwen/Qwen3-Embedding-4B"`, provisional until calibration), `EMBEDDING_OPENAI_MODEL` (str, `"text-embedding-3-large"`), `EMBEDDING_DIM` (int, 1024), `EMBEDDING_MODAL_URL` (str, `""`), `EMBEDDING_GPU_RATE` (float, `0.000164`, Modal's T4 per-second rate, 2026-11-24; L4 is `0.000222`), `EMBEDDING_MODAL_IDLE_SECONDS` (float, 10.0, which must equal the app's `scaledown_window`), `EMBEDDING_TIMEOUT` (int, 180; a cold start loads several GB of weights).

- [ ] **Step 1: Write the failing tests** (no DB; stub `post` returning objects with `status_code`, `json()` and `text`):
  - `test_embed_text_strips_the_google_suffix_and_a_repeated_body`: on a `https://news.google.com/rss/articles/x` URL, title `"Widget plant shut - Gizmo News Agency"` with body `"Widget plant shut  Gizmo News Agency"` gives `"Widget plant shut"`. A non-Google URL keeps the suffix. With a real body, the result is `"Widget plant shut\nGizmo output fell."`.
  - `test_embed_text_never_empties_a_real_title`: the title `" - Gizmo"` on a Google URL falls back to the unstripped title; a whitespace-only title gives `""`.
  - `test_modal_reply_with_another_prompt_version_is_bad_response`
  - `test_embed_text_caps_on_a_word_boundary`: a 5,000-char body gives at most 1,000 chars, and the result does not end mid-word.
  - `test_text_hash_is_stable_and_differs_by_text`
  - `test_pg_vector_round_trip`: `from_pg_vector(to_pg_vector([0.5, -0.25]))` equals `[0.5, -0.25]`.
  - `test_modal_chunks_and_preserves_order`: 300 texts give 3 posts of 128, 128 and 44, and the vectors come back in input order.
  - `test_modal_cold_call_is_charged_boot_and_idle`: `runtime_s=2, boot_s=20, cold_start=True, idle_s=10, rate=0.0001` gives `usd == 0.0032`. The same call warm gives `0.0002`.
  - `test_modal_reply_with_a_different_model_is_bad_response`
  - `test_error_kinds`: a timeout, 429 or 503 is `transient`; a 401, 403 or 404 is `config`; a 400, 413 or 422 is `input`; a `{"vectors": "x"}` body is `bad_response`. Each `EmbedError.calls` ends with an `outcome="failed"` record.
  - `test_openai_reorders_by_index_and_prices_from_usage`: `data` arrives with indexes [1, 0]; the vectors come back in input order; `usage.prompt_tokens=1_000_000` on `text-embedding-3-large` gives `usd == 0.13`.
  - `test_openai_sends_dimensions_and_bearer_key`
  - `test_active_embedder_names_what_is_missing`: provider `modal` with an empty URL gives `(None, "EMBEDDING_MODAL_URL is empty")`; provider `openai` with no key gives `(None, "OPENAI_API_KEY is not set")`.
  - `test_space_id_changes_with_dim`
- [ ] **Step 2: Run them** to see them fail.
- [ ] **Step 3: Implement** `embeddings.py` and the knobs.
- [ ] **Step 4: Run the gate.**
- [ ] **Step 5: Commit:** `feat(storylines): embedding interface with Modal and OpenAI providers (Refs news-brief-al7.7)`.

---

### Task 3: the Modal embedding service

**Files:**
- Create: `modal_apps/__init__.py` (empty), `modal_apps/embed_core.py`, `modal_apps/embed_service.py`, `modal_apps/README.md`, `tests/test_embed_core.py`

**Interfaces:**
- Produces, in `embed_core.py` (pure, with no `modal` or `torch` import):
  - `MAX_INPUTS = 128`, `MAX_SEQ_TOKENS = 512`.
  - `PROMPTS: dict[str, dict]`, keyed by model id. For `Qwen/Qwen3-Embedding-4B` and `-8B`: queries become `"Instruct: Given a topic description, retrieve news articles relevant to it\nQuery:{text}"`, and documents are sent as is. For `Octen/Octen-Embedding-8B`: no query prefix, and documents are sent as is.
  - `PROMPT_VERSION = 1`, the same number as `embeddings.EMBED_PROMPT_VERSION`. A test reads both files and asserts that they agree.
  - `parse_request(payload: dict, *, served_model: str, native_dim: int) -> Request | str`. It returns an error string for a missing or non-list `texts`, more than `MAX_INPUTS`, a `dim` outside `[32, native_dim]`, a `kind` other than `document` or `query`, a `model` ≠ `served_model`, or a `prompt_version` ≠ `PROMPT_VERSION`.
  - `prepare(texts, kind, model) -> list[str]` (applies `PROMPTS`).
  - `truncate_and_normalise(vec: list[float], dim: int) -> list[float]` (Matryoshka cut, then L2).
  - `response(*, model, dim, vectors, tokens, runtime_s, boot_s, cold_start) -> dict`, which also carries `prompt_version`.
  - `DTYPE_BY_GPU = {"T4": "float16", "L4": "bfloat16", "A10": "bfloat16"}`. A T4 has no fast bf16, and fp32 weights for a 4B model fill its 16 GB.
- `embed_service.py`:
  - The app is `modal.App(f"newsbrief-embed-{slug}")`, with the model and GPU read at deploy time from `EMBED_MODEL` and `EMBED_GPU` (default `T4`).
  - The image is `debian_slim(python_version="3.12")`. It pins `sentence-transformers==6.2.0` plus `torch`, `transformers` (≥ 4.51, which Qwen3 requires) and `fastapi[standard]`. Each pin is the latest release at least 14 days old when you write the file, checked with `pip index versions <pkg>` or the PyPI JSON `upload_time`; record the versions and dates in the README.
  - The class is `@app.cls(gpu=EMBED_GPU, enable_memory_snapshot=True, scaledown_window=10, max_containers=1, volumes={"/models": Volume.from_name("newsbrief-embed-models", create_if_missing=True)})`.
  - `@modal.enter(snap=True)` loads the model on CPU from `/models`, with `model_kwargs={"torch_dtype": DTYPE_BY_GPU[EMBED_GPU]}`. Missing weights download into the Volume once. The README records the measured GPU memory per candidate.
  - `@modal.enter(snap=False)` moves it to CUDA and records `boot_s` (time since process start).
  - The endpoint is `@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)`: `encode(payload) -> dict`. It calls `parse_request` and answers a bad request with HTTP 400 and the error string. It encodes with `normalize_embeddings=True`, then `truncate_and_normalise`. `tokens` is the summed attention-mask length; `runtime_s` is the handler's wall time; `cold_start` is true on the container's first call only.
  - Do **not** enable GPU snapshots (alpha, per Modal's docs on 2026-11-24).
- `README.md`: the deploy command per candidate, which goes to the Rollout section; the reminder that a changed `scaledown_window` must also go in `EMBEDDING_MODAL_IDLE_SECONDS`; and that the endpoint URLs go in a settings row and on the host, never in git.

- [ ] **Step 1: Write the failing tests** (`tests/test_embed_core.py`, importing `modal_apps.embed_core`):
  - `test_parse_request_rejects_each_bad_field`: one case per error above, each giving a string naming the field.
  - `test_parse_request_accepts_a_valid_request`
  - `test_qwen_queries_get_the_instruction_and_documents_do_not`
  - `test_octen_gets_no_prefix`
  - `test_truncate_and_normalise`: `[3, 4, 12]` cut to 2 gives `[0.6, 0.8]`.
  - `test_response_shape`: the keys are exactly `model, dim, vectors, tokens, runtime_s, boot_s, cold_start, prompt_version`.
  - `test_prompt_versions_agree`: `embed_core.PROMPT_VERSION == embeddings.EMBED_PROMPT_VERSION`.
- [ ] **Step 2: Run them** to see them fail; **Step 3: implement** `embed_core.py`, then write `embed_service.py` and the README. `embed_service.py` is not imported by tests; `ruff check` covers it.
- [ ] **Step 4: Run the gate.** Also confirm `tests/test_packaging.py` passes: `modal_apps` is not imported by any shipped module.
- [ ] **Step 5: Commit:** `feat(storylines): Modal embedding service, core tested (Refs news-brief-al7.7)`.

---

### Task 4: `group_store.py` and `grouping.py`

**Files:**
- Create: `group_store.py`, `grouping.py`, `tests/test_grouping.py` (pure parts), `tests/test_group_store.py` (DB-gated)

**Interfaces:**
- `group_store.py` (all SQL for 0020; never commits):
  - `@dataclass(frozen=True) class PendingItem: id: int; title: str; body: str | None; outlet: str; outlet_kind: str; created_at: datetime`
  - `@dataclass(frozen=True) class Candidate: group_id: int; similarity: float; title_similarity: float`
  - `@dataclass(frozen=True) class Calibration: space: str; join_sim: float; pair_sim: float; title_sim: float; retrieval_min_sim: float | None; weighted_precision: float; weighted_recall: float; pairs_labelled: int; prompt_version: int; judge_model: str; week_ending: date`
  - `record_calls(conn, calls: Sequence[CallRecord], *, purpose: str) -> None`
  - `items_missing_vectors(conn, *, space: str, limit: int, item_ids: Sequence[int] | None = None) -> list[PendingItem]`, oldest first by `(created_at, id)`. When `item_ids` is given, only those items are considered (pre-flight ruling R2: calibration embeds a snapshot week, not 81 days). It excludes items that have a skip row in the space. `PendingItem` also carries `url`, which `embed_text` needs.
  - `record_skip(conn, *, space: str, item_id: int, reason: str) -> None` (`ON CONFLICT DO NOTHING`).
  - `vectors_by_hash(conn, *, space: str, hashes: Sequence[str]) -> dict[str, str]` (hash to the stored vector as text).
  - `store_vectors(conn, *, space: str, dim: int, rows: Sequence[tuple[int, str, str]]) -> None`, where each row is `(item_id, text_hash, vector literal)`. It uses `ON CONFLICT DO NOTHING`.
  - `next_ungrouped(conn, *, space: str, limit: int) -> list[tuple[PendingItem, str | None]]`. Oldest first, skipping items that have a skip row; the second field is the item's vector literal, or None if it has none yet.
  - `candidate_groups(conn, *, space: str, vector: str, title: str, created_at: datetime, window_hours: int, limit: int = 5) -> list[Candidate]`. These are groups with `opened_at ≥ created_at − window`, considering only members with `joined_at ≤ created_at`. Per group it returns the maximum `1 − (embedding <=> vector)` and the maximum `similarity(member title, title)`, ordered by similarity descending.
  - `open_group(conn, *, space: str, item: PendingItem) -> int` and `join_group(conn, *, space: str, group_id: int, item: PendingItem, candidate: Candidate) -> None`. Both keep `outlet_count` (distinct outlets), `last_seen_at` and `has_primary` current.
    - `has_primary` means an outlet whose `kind` is `'primary'`: today ISW, 38 North and Bank of Japan, which are tagged that way in the feed list.
    - Spec §6.3's per-storyline watched sources come in plan 3 and are a different thing.
  - `latest_calibration(conn, space: str) -> Calibration | None` and `write_calibration(conn, cal: Calibration) -> None`.
  - `oldest_ungrouped_created_at(conn, *, space: str) -> datetime | None`
- `grouping.py`:
  - `@dataclass(frozen=True) class Thresholds: join_sim: float; pair_sim: float; title_sim: float` (from a `Calibration`).
  - `decide_group(cands: Sequence[Candidate], th: Thresholds) -> Candidate | None`. It returns the candidate with the highest similarity among those that pass `similarity ≥ join_sim` or `(similarity ≥ pair_sim and title_similarity ≥ title_sim)`. Ties go to the lower `group_id`.
  - `EMBED_CHUNK = 128`; `LIVE_MAX_ITEMS = 500`.
  - `space_lock(conn, space: str)`: a context manager over `db.advisory_lock(conn, f"group:{space}")`, yielding whether the lock was taken. `group_pass` uses it, and so do the backfill (Task 7) and calibration (Task 8), per pre-flight ruling R1.
  - `embed_pending(conn, embedder, *, space: str, dim: int, limit: int, purpose: str, item_ids: Sequence[int] | None = None) -> dict`. `item_ids` is passed through to `items_missing_vectors`.
    - It builds `embed_text`; a `""` text gets a skip row (`empty_text`).
    - It reuses vectors by `text_hash` from both the store and this batch, so identical texts make one provider input. The rest are embedded as `kind="document"`.
    - It records calls and stores vectors, committing after recording each call's spend and after each chunk's vectors.
    - **On an `input` error for a chunk,** it bisects: it halves the chunk and retries each half, down to single texts. That costs at most about 2 × the chunk size in extra calls, all recorded. A single text that still gets `input` gets a skip row with the provider's message, and the rest proceed. `transient`, `config` and `bad_response` never skip.
    - It returns `{"embedded", "reused", "skipped", "calls"}` and raises `EmbedError` after recording its calls.
  - `assign_pending(conn, *, space: str, thresholds: Thresholds, window_hours: int, limit: int) -> dict`. It walks `next_ungrouped` in order and **stops at the first item with no vector**. For each item it calls `candidate_groups`, then `decide_group`, then joins or opens. It commits every 100 items and returns `{"grouped", "opened", "joined", "stopped_at_missing_vector": bool}`.
  - `group_pass(conn, embedder, *, space: str, dim: int, window_hours: int, max_items: int, purpose: str = "items") -> dict`.
    - It holds `space_lock(conn, space)` for the whole pass, so the live job and a backfill of the same space never interleave. When another connection holds the lock it returns status `busy`.
    - It runs `embed_pending`, then (if a calibration exists) `assign_pending`, and returns `{"status", "error", "embedded", "reused", "skipped", "calls", "grouped", "opened", "joined"}`. Status is `ok`, `nothing_to_do`, `busy`, `uncalibrated` (vectors are still stored; the error names the space), `transient`, `config` or `bad_response`. On an embed error it still assigns items that already have vectors, since order is preserved by the stop rule.

- [ ] **Step 1: Write the failing tests.**
  - Pure (`tests/test_grouping.py`):
    - `test_decide_group_join_alone`: sim 0.9 ≥ join 0.85 joins.
    - `test_decide_group_pair_rule`: sim 0.75 with title 0.6 joins under `pair 0.7` and `title 0.5`; with title 0.4 it opens a new group.
    - `test_decide_group_prefers_highest_similarity_then_lowest_id`
    - `test_decide_group_none_when_nothing_passes`
  - DB-gated (`tests/test_group_store.py`, a fake embedder that maps text to a fixed vector):
    - `test_identical_text_is_embedded_once_and_groups_together`: two outlets with the same text send one input to the provider, store two rows, and land in one group with `outlet_count == 2`.
    - `test_assignment_stops_at_the_first_item_without_a_vector`: with items A < B < C by `created_at` and vectors for A and C only, one pass groups A, leaves B and C ungrouped, and reports `stopped_at_missing_vector`.
    - `test_an_item_never_joins_a_group_through_a_later_member`
    - `test_window_closes_after_72_hours`: an item 73 h after a group's opening opens a new group even at similarity 1.0.
    - `test_uncalibrated_space_embeds_but_does_not_assign`: status `uncalibrated`, the error contains the space id, vectors are stored, and there are 0 group rows.
    - `test_transient_error_records_the_failed_call_and_keeps_earlier_chunks`: the second chunk raises a 503. The first chunk's vectors and both call rows (`ok`, `failed`) are committed, and status is `transient`.
    - `test_has_primary_and_outlet_count`
    - `test_an_unembeddable_item_is_skipped_and_the_queue_moves`: a 128-text chunk where the fake provider answers 400 whenever text 37 is present. Text 37 gets a skip row with the message, the other 127 are stored, and the next item after 37 is grouped.
    - `test_a_blank_title_is_skipped_without_a_call`
    - `test_a_second_pass_on_the_same_space_reports_busy` (hold the lock on another connection)
    - `test_spaces_do_not_mix`: groups in space `a@2` are invisible to `candidate_groups` in `b@2`.
- [ ] **Step 2: Run them** to see them fail. **Step 3: Implement.** **Step 4: Run the gate.**
- [ ] **Step 5: Commit:** `feat(storylines): event groups from embeddings, in capture order (Refs news-brief-al7.7)`.

---

### Task 5: `retrieval.py`, hybrid search with `as_of`

**Files:**
- Create: `retrieval.py`, `tests/test_retrieval.py` (pure RRF plus DB-gated)
- Modify: `group_store.py` (the retrieval SQL lives there)

**Interfaces:**
- `group_store.py` adds:
  - `query_vector(conn, *, space, text_hash) -> str | None` and `store_query_vector(conn, *, space, dim, text_hash, vector) -> None`.
  - `vector_hits(conn, *, space, vector: str, since: datetime, as_of: datetime, min_similarity: float, limit: int) -> list[tuple[int, float]]` gives `(group_id, best similarity)`. It considers only members with `since < joined_at ≤ as_of`.
  - `lexical_hits(conn, *, space, query: str, since, as_of, min_score: float, limit: int) -> list[tuple[int, float]]`. The score is `word_similarity(item_group_members.title, query)`, so a short title found inside a long query scores high. That column holds the cleaned title, with the Google " - Publisher" stripped (Task 4 fix round 1, `embeddings.clean_title`).
  - `groups_as_of(conn, *, group_ids: Sequence[int], as_of: datetime) -> dict[int, GroupView]`, where `GroupView(group_id, title, outlets: tuple[str, ...], outlet_count, perspectives: tuple[str, ...], has_primary, first_seen, last_seen, item_ids: tuple[int, ...])` is computed **only from members with `joined_at ≤ as_of`**.
- `retrieval.py`:
  - `RRF_K = 60`, `LEXICAL_MIN = 0.5` (provisional; Task 8 reports the lexical arm's precision), `PER_QUERY_LIMIT = 50`.
  - `@dataclass(frozen=True) class GroupHit: group: GroupView; score: float; best_similarity: float | None; best_title_score: float | None`
  - `@dataclass(frozen=True) class SearchResult: hits: list[GroupHit]; degraded: bool; reason: str | None`
  - `rrf(rank_lists: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]` (pure).
  - `query_vectors(conn, embedder, *, space, dim, queries: Sequence[str]) -> list[str]`. The cache key is `text_hash(f"query/v{EMBED_PROMPT_VERSION}:{text}")`. A miss embeds with `kind="query"` and records calls with `purpose="queries"`.
  - `search_groups(conn, embedder: Embedder | None, *, space: str, dim: int, queries: Sequence[str], since: datetime, as_of: datetime, limit: int = 60, min_similarity: float | None = None) -> SearchResult`.
    - One vector list and one lexical list per query, fused by `rrf`, then `groups_as_of`.
    - `min_similarity` defaults to the space's `Calibration.retrieval_min_sim`. While that is NULL, which it is until plan 2 fits it, the vector arm has **no floor** and only rank counts. That is not a degraded result.
    - An `EmbedError`, or `embedder is None`, gives a trigram-only result with `degraded=True` and the reason. It never raises for a provider fault.

- [ ] **Step 1: Write the failing tests:**
  - `test_rrf_orders_by_fused_rank`: lists `[1,2,3]` and `[3,1]` rank 1 first, then 3, then 2.
  - `test_as_of_hides_later_members_and_their_outlets` (DB-gated): a group with a Widget Wire member at T and a Gizmo Daily member at T+2h, queried with `as_of = T+1h`, gives `outlet_count == 1`, `last_seen == T` and one item id.
  - `test_group_first_seen_after_as_of_is_absent`
  - `test_since_bounds_by_member_time_not_group_open`: a group opened before `since` with a new member after it is returned.
  - `test_query_vectors_are_cached_by_hash`: a second search with the same query makes no provider call. With `EMBED_PROMPT_VERSION` monkeypatched to 2, it makes one call.
  - `test_provider_fault_degrades_to_trigram`: the embedder raises a transient `EmbedError`; the result is `degraded` with the reason, and the lexical hits are returned.
  - `test_min_similarity_floor_drops_weak_vector_hits`
  - `test_null_floor_keeps_ranked_vector_hits_and_is_not_degraded`
- [ ] **Step 2–4:** fail, implement, then run the gate.
- [ ] **Step 5: Commit:** `feat(storylines): hybrid group retrieval with an as_of cut (Refs news-brief-al7.7)`.

---

### Task 6: the `group` job, its alert, and the weekly cost line

**Files:**
- Modify: `brief.py` (`mode_group`, `MODES`, `JOB_MODES`, `GROUP_ALERT_KEY`, and a line after the weekly summary), `scheduler.py` (`Schedule("group", "interval", None, 30, grace_minutes=10)`), `common.py` (`STORYLINE_GROUPING_ENABLED`, bool False; `STORYLINE_GROUP_WINDOW_HOURS`, int 72), `docker-compose.yml`, `Dockerfile` (COPY the four new modules), `.github/workflows/docker-publish.yml` (the four `paths:` entries, **and** the explicit file lists of its `ruff check` and `ruff format --check` steps, which `test_every_copied_module_is_linted_in_ci` enforces, plus `modal_apps/**` in `paths:`), `group_store.py` (`embedding_month_to_date`)
- Test: `tests/test_group_job.py`; extend `tests/test_scheduler.py` if it enumerates schedules

**Interfaces:**
- `group_store.embedding_month_to_date(conn, *, now: datetime) -> dict[str, dict]` maps each provider to `{"usd", "openai_equivalent_usd", "calls", "cold_starts", "tokens"}` for the calendar month of `now`, in UTC.
- `brief.GROUP_BACKLOG_ALERT_HOURS = 2`.
- `brief.mode_group() -> None`:
  - It reads `STORYLINE_GROUPING_ENABLED`, `STORYLINE_GROUP_WINDOW_HOURS`, `EMBEDDING_DIM` and `active_embedder()` once. Off logs `Group: off`.
  - It runs `grouping.group_pass(…, max_items=grouping.LIVE_MAX_ITEMS)` and logs one line: `Group: status=… embedded=… reused=… grouped=… opened=… joined=… calls=… backlog_h=…`.
  - **It rings with `_alert_once(GROUP_ALERT_KEY, …, "🧩")`** on `config`, `bad_response` or `uncalibrated` straight away, and on `transient` only when the oldest ungrouped item is more than `GROUP_BACKLOG_ALERT_HOURS` old. The message names the status, the error and the backlog age.
  - It clears the alert on `ok` or `nothing_to_do` with the backlog under the threshold. `busy` (a backfill holds the space) neither rings nor clears.
  - An `active_embedder()` problem counts as `config` and carries its message.
  - No call is made then, so there is no `embedding_calls` row. The record of that run is the `job_runs` ledger row, the log line and the alert, which is how spec §6.1's "a decision row, not a silent skip" is met on that path.
- `brief.embedding_cost_line(conn, *, now) -> str | None` gives `"🧮 Embeddings this month: Modal $X.XX (N calls, C cold starts); the same tokens on OpenAI ≈ $Y.YY."`, plus `" OpenAI: $Z.ZZ."` when OpenAI spend exists. It gives None when there were no calls. `mode_weekly` sends it with `common.telegram_send` after the summary is delivered. The line is fixed-size, so the bare send is allowed (memory: `telegram-send-long-convention`).

- [ ] **Step 1: Write the failing tests** (stub `group_pass`, `active_embedder` and `_alert_once`; patch all three send channels):
  - `test_mode_group_off_does_nothing`
  - `test_mode_group_rings_on_config_and_names_the_problem`: an empty `EMBEDDING_MODAL_URL` rings once, with that text.
  - `test_mode_group_rings_on_uncalibrated`
  - `test_transient_rings_only_when_the_backlog_is_old`: at 1 h no ring; at 3 h a ring.
  - `test_ok_with_no_backlog_clears_the_alert`
  - `test_group_is_scheduled_every_30_minutes_and_is_a_job_mode`
  - `test_embedding_cost_line_shows_both_providers_and_the_equivalent` (DB-gated: two `embedding_calls` rows this month, one last month).
  - `test_weekly_sends_the_cost_line_after_the_summary`
- [ ] **Step 2–4:** fail, implement, then run the gate. That includes `tests/test_packaging.py`: Dockerfile COPY, the workflow paths, and compose lines for every new knob and for `OPENAI_API_KEY`.
- [ ] **Step 5: Commit:** `feat(storylines): group job every 30 minutes, backlog alert, weekly cost line (Refs news-brief-al7.7)`.

---

### Task 7: `scripts/embed_backfill.py`

**Files:**
- Create: `scripts/embed_backfill.py`, `tests/test_embed_backfill.py`
- Modify:
  - `backup.py`: add `--exclude-table-data=item_embeddings` and `--exclude-table-data=query_embeddings` to the `pg_dump` call. Vectors are a cache, rebuilt from the items by this script in about an hour. Left in, they are dumped as text at about 10 KB a row, multiplying every nightly dump with 14 kept.
  - `tests/test_backup.py`: assert both flags are present.
  - `Dockerfile`: the `COPY scripts/` comment no longer says TEMPORARY. Its consumers are now the backfill, the calibration and the probe. Add a note to bead `news-brief-115` saying so: `bd update news-brief-115 --append-notes …`.

**Interfaces:**
- `main(argv=None, *, embedder=None, post=None, get=None, sleep=time.sleep) -> int`, run as `--space-from-settings | --provider {modal,openai} --model M --dim D [--url U]`, plus `[--max-items N] [--no-group] [--openai-batch] [--max-usd X]`.
  - **Modal (or OpenAI synchronous):** loops `embed_pending(purpose="backfill", limit=5000)` until done, printing `embedded=… reused=… calls=… usd=…` per round. Unless `--no-group`, it then loops `assign_pending` for the space (when calibrated) and prints the group totals.
  - **`--openai-batch`** (only with `--provider openai`), half price, for bulk work (spec §21.2):
    - It writes JSONL to `DATA_DIR/embed_backfill/<space-slug>/`, one request per 100 items (`custom_id = "chunk-<n>"`, body `{"model", "input": [...], "dimensions"}`), and saves the chunk-to-items map beside it.
    - It uploads (`POST /v1/files`, `purpose=batch`), then creates `POST /v1/batches` with `endpoint=/v1/embeddings` and `completion_window=24h`.
    - It splits the work into batches of at most 50,000 inputs each, OpenAI's limit (spec §21.2). About 64k items means two batches.
    - It saves each batch id in `state.json`, one entry per batch. A rerun resumes polling instead of submitting again.
    - When the batch completes, it downloads the output, stores the vectors and records one `CallRecord` per chunk with `batch=True` and `usd` at `OPENAI_BATCH_FACTOR`.
    - It refuses to submit when the estimated cost (chars ÷ 4 tokens) exceeds `--max-usd` (default 5).
  - **The target space may differ from the active one,** so a new space can be filled and grouped before the switch.
  - **It holds `grouping.space_lock(conn, space)` for its whole run** (pre-flight ruling R1). When the lock isn't taken, it prints `busy: another pass holds <space>` and exits 4.
  - Exit codes: 0 done; 1 provider error, with the reason printed; 2 usage error; 3 a batch still in progress (rerun later); 4 busy.

- [ ] **Step 1: Write the failing tests** (`tmp_path` as `DATA_DIR`, stub HTTP):
  - `test_modal_backfill_embeds_then_groups_and_prints_totals`
  - `test_openai_batch_submits_once_and_resumes_polling` (`state.json` exists, so there is no second upload)
  - `test_openai_batch_splits_above_50000_inputs` (with `--max-items` and a small cap monkeypatched, two state entries)
  - `test_backup_excludes_embedding_data` (in `tests/test_backup.py`)
  - `test_openai_batch_output_maps_back_by_custom_id_and_is_priced_at_half`
  - `test_refuses_above_max_usd`
  - `test_backfill_into_a_non_active_space_leaves_the_active_space_untouched`
  - `test_backfill_exits_4_when_the_space_lock_is_held` (hold it on another connection)
- [ ] **Step 2–4:** fail, implement, then run the gate.
- [ ] **Step 5: Commit:** `feat(storylines): embedding backfill, with OpenAI Batch API for bulk (Refs news-brief-al7.7)`.

---

### Task 8: `scripts/embed_calibrate.py`, model pick and thresholds

**Files:**
- Create: `scripts/embed_calibrate.py`, `tests/test_embed_calibrate.py`
- Modify:
  - `comprehend.py`: `PRICES_PER_MTOK` gains `"claude-opus-5-5": (4.00, 20.00)` and `"claude-sonnet-5-5": (2.00, 10.00)`, from the pricing page 2026-11-24.
  - `group_store.py`: add `nearest_items(conn, *, space, vector: str, since: datetime, until: datetime, limit: int, exclude_ids: Sequence[int] = ()) -> list[tuple[int, float]]`, item-level cosine neighbours among items with `created_at` in `[since, until)` (pre-flight ruling R3). Pair sampling and the retrieval check both use it.
- Calibration embeds only the snapshot's items (`embed_pending(..., item_ids=snapshot ids)`) and holds `grouping.space_lock` for each space while it embeds (ruling R2).

**Interfaces:**
- `main(argv=None, *, embedders=None, post=None) -> int`, run as `--week-ending YYYY-MM-DD --space SPEC [--space SPEC …] [--pairs 600] [--judge-model claude-opus-5-5] [--accept SPACE]`. `SPEC` is `modal:<model>@<dim>=<url>` or `openai:<model>@<dim>`. An `openai:` spec requires `--include-openai`, and the script prints that it sends one week of titles and summaries, and the generated queries, to OpenAI (spec §19).
- Steps, each resumable from files under `DATA_DIR/embed_calibration/<week-ending>/`:
  1. **Snapshot:** items created in `[week_ending − 7 d, week_ending)`, written once to `snapshot.json` and reused. Print `items=N`. Storylines are not read.
  2. **Embed** every snapshot item into each space, through `embed_pending(purpose="calibration")`, so the vectors stay in `item_embeddings` for a later backfill to reuse.
  3. **Pairs:** for each space, each item's nearest neighbours within 72 h, bucketed into similarity bands of 0.05 from 0.30 to 1.00. Sample evenly per band across the union of spaces up to `--pairs`, deduplicated, with a fixed seed of 0. Write them to `pairs.json`.
  4. **Label** each pair with the judge: "same event (one happening, not the same topic), different, or unclear". This is a forced tool call, 20 pairs per call, with labels `P1…P20` mapped back in code. Results are appended to `labels.jsonl`. Spend goes to `comprehend_spend` under stage `calibrate`. `unclear` is excluded from scoring and counted.
  5. **Grouping scores per space, weighted to the real mix.** Band-stratified sampling over-represents the rare similarities, so easy negatives would flatter an unweighted score. Each labelled pair is therefore weighted, per space, by (that space's share of all 72 h nearest-neighbour pairs in the pair's band) ÷ (labelled pairs in that band). Every metric below is weighted:
     - `join_sim` = the lowest threshold whose weighted precision for vector-alone joins is ≥ 0.95;
     - `pair_sim` and `title_sim` by grid search, at steps of 0.05, for the highest weighted F1 with weighted precision ≥ 0.90;
     - the combined rule's weighted precision, recall and F1, and the unweighted AUC, reported only.
     - **Pre-registered bar:** weighted precision ≥ 0.90 **and** weighted recall ≥ 0.60 at the chosen thresholds. Below it, the space is marked `grouping unreliable`.
  6. **Retrieval check per space, with production-shaped queries.** The editor's queries are its document fields (open questions, actors, constraints, consensus, signposts; spec §7.3), not storyline names, and the live storylines are to be re-seeded in plan 2 (§13). So the queries are generated:
     - Sample 20 **situations**: items in the week with a near-duplicate from another outlet (similarity > 0.8 in any space), with a fixed seed of 0 and at most 2 per outlet.
     - For each, the judge reads the item and its near-duplicates and writes 3 queries, one per style: an open question, an actor and what it wants, and a constraint. It is told not to reuse the headline's distinctive words. That gives 60 queries, labelled `Q1…`.
     - Pool the top 30 items per query per space over the whole week, excluding the situation's own items, and judge each (query, item) pair as relevant or not, 30 per call, in real time through `brief._post_messages` (pre-flight ruling R4: the repo has no multi-request tool-call batch helper, and building one saves about $2 once).
     - Report per space: nDCG@30 and pooled recall@30, and the lexical arm's precision at `LEXICAL_MIN`.
     - **No retrieval floor is fitted here:** `retrieval_min_sim` stays NULL and is fitted in plan 2 on real document queries over the replay.
  7. **Report:** one row per space, with the thresholds, weighted P/R/F1, AUC, the bar verdict, nDCG@30, recall@30 and the unclear count, plus the judge's spend. Then a recommendation by the **pre-registered rule**:
     - Leave out every space marked `grouping unreliable`.
     - Of the rest, take the highest mean nDCG@30.
     - Prefer a space on a cheaper GPU when the 95% bootstrap interval of (best − it), with 1,000 resamples over queries and a seed of 0, includes 0.
     - If every space is unreliable, recommend none and exit 1.
  8. **`--accept SPACE`** writes the `embedding_calibrations` row for that space (with `prompt_version`), and prints the `INSERT … ON CONFLICT` statements that make it active (`EMBEDDING_PROVIDER`, `EMBEDDING_MODAL_MODEL` or `EMBEDDING_OPENAI_MODEL`, `EMBEDDING_DIM`, `EMBEDDING_MODAL_URL`, `EMBEDDING_GPU_RATE`).
- It never prints a title, a generated query, an article text or a URL, and it reads no storylines.
- Expected judge spend is about $4–8: 30 calls for the pairs, 3 calls to write the queries, and about 100 calls for relevance, all real time.

- [ ] **Step 1: Write the failing tests** (stub embedders and judge; invented items):
  - `test_auc_perfect_and_random`: separable scores give 1.0; identical scores give 0.5.
  - `test_band_weights_restore_the_natural_mix`: with 90% of natural pairs in the lowest band and an equal sample per band, weighted precision equals the hand-computed natural-mix value, not the unweighted one.
  - `test_join_threshold_is_lowest_with_weighted_precision_95`
  - `test_grid_search_respects_the_precision_floor`
  - `test_bar_marks_low_recall_unreliable`
  - `test_ndcg_at_30`: a hand-computed 3-item case.
  - `test_pairs_are_band_stratified_and_seeded`
  - `test_situations_exclude_their_own_items_from_scoring`
  - `test_judge_labels_map_back_and_unknown_label_is_an_error`
  - `test_snapshot_and_labels_are_reused_on_rerun` (the second run makes no judge calls)
  - `test_report_never_prints_titles_or_queries`
  - `test_recommendation_prefers_a_cheaper_gpu_only_when_the_bootstrap_interval_includes_zero`
  - `test_all_unreliable_recommends_none_and_exits_1`
  - `test_accept_writes_the_calibration_row`
  - `test_openai_space_requires_include_openai`
- [ ] **Step 2–4:** fail, implement, then run the gate.
- [ ] **Step 5: Commit:** `feat(storylines): embedding calibration, model pick and thresholds (Refs news-brief-al7.7)`.

---

### Task 9: the fetch probe with a real extractor

**Files:**
- Modify: `requirements.txt` (`trafilatura==2.3.1`, released 2026-10-06; 2.4.0 is from 2026-11-23, too new), `scripts/storylines_fetch_probe.py`, `tests/test_storylines_fetch_probe.py`

**Interfaces:**
- `probe_one(url, get, post=None, extract=None) -> tuple[str, int, str, int]` gives `(category, extracted chars, google, paragraph chars)`.
  - `extract(html) -> str` defaults to `trafilatura.extract(html, include_comments=False) or ""`.
  - The category is now decided by the **extracted** text. `paragraph_text` stays, for the comparison column.
- `report` adds a `usable_by_paragraphs` column (the old measure), so both are read side by side, and `median_chars_usable` uses the extracted length.
- The 13 existing tests keep passing, adjusted to the 4-tuple.

- [ ] **Step 1: Failing tests:**
  - `test_category_uses_the_extractor`: a page with no `<p>` but extractable `<article>` text is `usable`.
  - `test_report_has_both_usable_columns`
- [ ] **Step 2–4:** fail, implement, then run the gate. Check the image builds locally (`docker build .`) with the new dependency.
- [ ] **Step 5: Commit:** `feat(storylines): fetch probe measures with trafilatura (Refs news-brief-al7.7)`.

---

### Task 10: sync the spec and the docs

**Files:**
- Modify: the spec (§6.1: "switching is a settings change plus a re-embed **and a calibration** of the new space; thresholds live in `embedding_calibrations`, per space"; §18: add `STORYLINE_GROUPING_ENABLED`, which runs `group` independently of `STORYLINES_ENABLED`, and the embedding knobs as built; §22: plan 1 took `0020`, so plan 2's tables are `0021`, plus the as-built 0020 tables including `item_embed_skips`; §6.1: embedding vectors are a rebuildable cache, left out of backups; §15/§21: plan 2 fits `retrieval_min_sim` on real document queries, and plan 1's Task 8 picks the model with generated queries), `CLAUDE.md` (already done in Task 1; check), and memory `brief-local-run.md` (the image line) in the live corpus.
- [ ] **Step 1:** Make the edits. Grep the spec for every knob name added in Tasks 2 and 6; each must appear in §18.
- [ ] **Step 2: Commit:** `docs(storylines): spec synced to plan 1 as built (Refs news-brief-al7.7)`.

---

## Rollout (the operator)

**When:** after `/jobs` shows today's collect ✅ finished, and before 04:30 UTC; not around Sunday 21:00. Step 0 takes the bot down for a few minutes.

**Step 0: the image swap, before any plan-1 commit is pushed.** It needs no new code. The controller holds every plan-1 commit locally until you paste step 0's output.

1. Stop the app first, so nothing writes during the swap, then back up and record the state:
   ```bash
   docker compose stop newsbrief
   docker compose run --rm --no-deps newsbrief backup
   docker compose exec postgres psql -U newsbrief -d newsbrief -c "SELECT count(*) FROM items" -c "SELECT datlocprovider, datcollate, datctype FROM pg_database WHERE datname = current_database()" -c "SHOW data_directory" -c "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
   docker inspect $(docker compose ps -q postgres) --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
   ```
   - The commands here use plain `docker compose`. On the host, use its own invocation (its compose file and env files).
   - **`data_directory` must be `/var/lib/postgresql/18/docker`,** the new image's default (measured 2026-11-24), and it must sit under one of the mount destinations printed. If it differs, stop and paste: the new image would start an empty cluster.
   - `rolsuper` must be `t`: `vector` is not a trusted extension, so 0020 needs a superuser.
   - Note the current `image:` line, for a rollback.
2. In the host's compose file, set the `postgres:` image to
   `pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa`.
   That is the tag's multi-architecture index digest, read from Docker Hub on 2026-11-24; Task 1 pins the same string. Change nothing else, the volume line least of all. Then:
   ```bash
   docker compose pull postgres && docker compose up -d postgres
   ```
3. Rebuild and verify the indexes. The C library changed from musl to glibc, so text indexes built under musl may be out of order. A row count cannot see that; amcheck can:
   ```bash
   docker compose exec postgres psql -v ON_ERROR_STOP=1 -U newsbrief -d newsbrief \
     -c "CREATE EXTENSION IF NOT EXISTS amcheck" \
     -c "REINDEX DATABASE newsbrief" \
     -c "SELECT count(*) AS indexes_checked FROM (SELECT bt_index_check(c.oid, true) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am a ON a.oid = c.relam WHERE a.amname = 'btree' AND c.relpersistence <> 't' AND i.indisvalid) x" \
     -c "SELECT count(*) FROM items"
   ```
   - `bt_index_check` raises on the first damaged index. A clean finish that prints `indexes_checked` above 0 is the pass.
   - The item count must equal step 1's. The app was stopped, so nothing was captured in between.
   - `-v ON_ERROR_STOP=1` makes psql stop and exit non-zero at the first error; without it psql carries on and exits 0.
   - There is no `REFRESH COLLATION VERSION` step. musl records no collation version (NULL), and Postgres refuses to refresh from NULL with `invalid collation version change`, an error after a swap that worked (measured on a test cluster moved from alpine to this image, 2026-11-24). The REINDEX is what matters.
   - If either check fails, stop: put the old image back, start it, and run `REINDEX DATABASE newsbrief` again under it, because indexes rebuilt here are in glibc order. The data volume is unchanged, and step 1's backup covers anything worse.
   - **Done 2026-11-25** by the operator, who checked every output against these criteria.
4. Start the **current** app again: `docker compose up -d newsbrief`. It has no 0020 yet; check `/jobs` is normal. Paste steps 1–4's output; the controller then pushes plan 1.

**After plan 1 is pushed and built:**

5. **Deploy the app:** `docker compose pull newsbrief && docker compose up -d newsbrief`. Then `grep "Migration 0020" newsbrief.log` should show `up applied`. Grouping is off.
6. **Deploy the Modal candidates** from the dev machine, with the `modal` CLI logged in to the workspace that serves the podcast endpoint:
   ```bash
   EMBED_MODEL=Qwen/Qwen3-Embedding-4B EMBED_GPU=T4 modal deploy modal_apps/embed_service.py
   EMBED_MODEL=Qwen/Qwen3-Embedding-8B EMBED_GPU=L4 modal deploy modal_apps/embed_service.py
   EMBED_MODEL=Octen/Octen-Embedding-8B EMBED_GPU=L4 modal deploy modal_apps/embed_service.py
   ```
   Keep the three URLs on the host, not in git.
   - **Check the services refuse strangers.** From anywhere, a POST without the `Modal-Key`/`Modal-Secret` headers, and one with made-up values, must both get `401`.
   - **Check the host's proxy tokens reach each service.** Run this inside the app container, which already holds `MODAL_PROXY_KEY` and `MODAL_PROXY_SECRET`, through the app's own client. Pass each deployment its own model (another model gets a `409`):
   ```bash
   docker compose run --rm --no-deps --entrypoint python newsbrief -c '
   import os, embeddings as e
   for url, model in [("<url1>", "Qwen/Qwen3-Embedding-4B"), ("<url2>", "Qwen/Qwen3-Embedding-8B"), ("<url3>", "Octen/Octen-Embedding-8B")]:
       m = e.ModalEmbedder(url, model, 1024, key=os.environ.get("MODAL_PROXY_KEY", ""), secret=os.environ.get("MODAL_PROXY_SECRET", ""), gpu_rate=0.000222, idle_s=10, timeout=900)
       try:
           v, calls = m.embed(["Widget test"], kind="document")
           print(model, "OK dim", len(v[0]), "cold_start", calls[-1].cold_start)
       except e.EmbedError as x:
           print(model, "FAILED", x.kind, str(x)[:200])
   '
   ```
   - Each line should read `OK dim 1024`. The first call takes minutes: a cold start plus the weight download.
   - `FAILED config HTTP 401` means the tokens belong to another workspace: create proxy tokens in this one.
   - **Done 2026-11-25:** both checks passed for all three services.
7. **Calibrate** on the host. It costs about $4–8 of Opus judging and cents of GPU time:
   ```bash
   docker compose run --rm --entrypoint python newsbrief scripts/embed_calibrate.py --week-ending <a recent Monday> \
     --space "modal:Qwen/Qwen3-Embedding-4B@1024=<url1>" --space "modal:Qwen/Qwen3-Embedding-8B@1024=<url2>" \
     --space "modal:Octen/Octen-Embedding-8B@1024=<url3>" --space "modal:Octen/Octen-Embedding-8B@4096=<url3>" \
     --gpu "modal:Qwen/Qwen3-Embedding-4B@1024=T4" --gpu "modal:Qwen/Qwen3-Embedding-8B@1024=L4" \
     --gpu "modal:Octen/Octen-Embedding-8B@1024=L4" --gpu "modal:Octen/Octen-Embedding-8B@4096=L4"
   ```
   The `--gpu` flags feed the cheaper-GPU tie-break and set the `EMBEDDING_GPU_RATE` the accepted space is priced at. Without them an L4 pick stays priced at the T4 rate, and the 🧮 line reads about 26% low.
   Add `--include-openai --space "openai:text-embedding-3-large@1024"` only if you are content to send one week of titles, summaries and the generated queries to OpenAI. It tells you whether switching later costs quality.
   Paste the report; the recommendation follows the pre-registered rule. Then rerun the same command with `--accept <space>`. It writes the calibration row, and it writes every activation statement to a file in the week's folder, the Modal URL included (which it does not print). Apply that file from the host:
   ```bash
   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U newsbrief -d newsbrief < "<the file>"
   ```
   `--accept` prints the file's path inside the container (`/app/logs/embed_calibration/<week>/activate-….sql`); on the host that is `${APPDATA_DIR}/news-brief/embed_calibration/<week>/activate-….sql`.
   - **Expected:** at least one space meets the grouping bar (weighted precision ≥ 0.90, recall ≥ 0.60). If none does, stop: grouping by embedding is unreliable on this data, and that is decided with the controller before plan 2.
   - The bar is measured on item pairs. The live job joins an item to a group on the best match across that group's members, so real groups' precision is at or below the bar rather than measured by it. Plan 2's grouping audit taps (spec §15.4) measure real groups.
8. **Backfill** the chosen space: `docker compose run --rm --entrypoint python newsbrief scripts/embed_backfill.py --space-from-settings`.
   - This is an estimate, measured when it runs: about 64k items take 1–2 h of T4 time, about $1, or more on an L4 for an 8B model.
   - Paste the totals: every item embedded or skipped, and the groups opened and joined.
   - Items captured while it runs are reported as `captured_during_run=K` and left to the live job; they never fail the run. Grouping alone measured 65.6 ms per item (5,000 synthetic items at 1024 dims, final review 2026-11-24), so allow about 70 minutes of grouping for 64k items on top of the embedding, and more at 4096 dims.
   - Then run `docker compose run --rm --no-deps newsbrief backup` and paste the new dump's size beside the previous night's. Vectors are excluded from dumps, so the two should be close.
9. **Switch grouping on:**
   ```sql
   INSERT INTO settings (key, value) VALUES ('STORYLINE_GROUPING_ENABLED', 'true')
   ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value = EXCLUDED.value, updated_at = now();
   ```
10. **The next day, check the effect:**
    - `grep "Group:" newsbrief.log | tail -5` shows `status=ok` with `backlog_h` under 1;
    - `embedding_calls` holds rows with `cold_start` true on most calls (30-minute spacing).
11. **After a week, reconcile the Modal cost.** The 🧮 line is a model of the bill: `boot_s` starts at process start, and the idle tail is assumed. Put Sunday's 🧮 Modal figure beside the same week on Modal's usage page and paste both. If they differ by more than 25%, the controller corrects the cost model before the switch to OpenAI is ever decided on it.
12. **Re-run the fetch probe:** `docker compose run --rm --entrypoint python newsbrief scripts/storylines_fetch_probe.py --per-outlet 5`. Use 5, not 15, to keep Google decodes low (spec §6.2). Paste the output; the controller updates spec §6.2.
13. Close `news-brief-bqa.7`. Plan 2 (the editor and the replay) is written next, against the code as built.

## Not in this plan

- The editor, the document tables, candidates, the replay and the judge: plan 2. That plan also fixes `comprehend.cost_usd`'s cache-read multiplier for Sonnet 5.5 and Opus 5.5, which is 0.05× rather than 0.1×.
- Article fetching for the editor (`read_article`, the per-outlet outcome record, the Google decode cap): plan 2, using Task 9's extractor.
- Alerts, bursts and the grouping audit taps (spec §15.4): plans 2 and 4.
- An ANN index on vectors: added only if a measured query is slow.
