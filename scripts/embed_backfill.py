"""Embed (and group) every item for one embedding space (storylines plan 1, Task 7).

Fills `item_embeddings` for a space, then, unless `--no-group`, assigns the items
to groups when the space is calibrated. The target space may differ from the
active one, so a new space can be filled and grouped before the switch.

On the host:

    docker compose run --rm --entrypoint python newsbrief \\
        scripts/embed_backfill.py --space-from-settings
    docker compose run --rm --entrypoint python newsbrief \\
        scripts/embed_backfill.py --provider openai --model text-embedding-3-large \\
        --dim 1024 --openai-batch

`--openai-batch` uses the OpenAI Batch API (half price, up to 24 h). It splits the
work into batches of at most 50,000 inputs, saves each batch id in `state.json`,
and a rerun resumes polling instead of submitting again. State lives under
`common.DATA_DIR/embed_backfill/<space slug>/`.

The script holds `grouping.space_lock` for its whole run, so it never interleaves
with the live group pass.

Exit codes: 0 done; 1 provider or database error; 2 usage error (or a refused
estimate); 3 a batch still in progress, rerun later; 4 busy, another pass holds
the space.

It prints counts, spend and ids only: never an article title, body or URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402  (path shim above must run first)

import common  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import group_store as gs  # noqa: E402
import grouping  # noqa: E402
from embeddings import (  # noqa: E402
    CallRecord,
    EmbedError,
    OpenAIEmbedder,
    embed_text,
    text_hash,
    to_pg_vector,
)

OPENAI_BASE = "https://api.openai.com/v1"
ROUND_ITEMS = 5000  # items per synchronous round
BATCH_CHUNK = 100  # inputs per batch request line
BATCH_MAX_INPUTS = 50_000  # OpenAI's limit on inputs in one batch (spec 21.2)
POLL_INTERVAL_S = 60
DEFAULT_WAIT_S = 1800  # how long one run polls before exiting 3
DEFAULT_MAX_USD = 5.0
HTTP_TIMEOUT_S = 120
PURPOSE = "backfill"
ASSIGN_ROUND = 5000
# Statuses after which a batch will never complete.
BATCH_DEAD = ("failed", "expired", "cancelled")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_IN_PROGRESS, EXIT_BUSY = 0, 1, 2, 3, 4

_URL = re.compile(r"https?://\S+")


class Failure(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _scrub(text: str, limit: int = 200) -> str:
    """A provider message with URLs removed, for printing."""
    return _URL.sub("<url>", str(text))[:limit]


def space_slug(space: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", space)


# ---- configuration ----


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="embed_backfill", description="Embed and group items for a space."
    )
    p.add_argument("--space-from-settings", action="store_true")
    p.add_argument("--provider", choices=("modal", "openai"))
    p.add_argument("--model")
    p.add_argument("--dim", type=int)
    p.add_argument("--url", help="Modal endpoint (default: the settings knob)")
    p.add_argument("--max-items", type=int, default=0, help="0 = no cap")
    p.add_argument("--no-group", action="store_true")
    p.add_argument("--openai-batch", action="store_true")
    p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    p.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_WAIT_S,
        help="seconds to poll a batch before exiting 3",
    )
    ns = p.parse_args(argv)
    explicit = (ns.provider, ns.model, ns.dim)
    if ns.space_from_settings == any(x is not None for x in explicit):
        p.error("give --space-from-settings, or --provider with --model and --dim")
    if not ns.space_from_settings and None in explicit:
        p.error("--provider, --model and --dim go together")
    if ns.max_items < 0 or ns.wait < 0 or ns.max_usd < 0:
        p.error("--max-items, --wait and --max-usd must not be negative")
    return ns


def _build_embedder(ns):
    """(embedder, problem): the credentials always come from the environment."""
    if ns.space_from_settings:
        return embeddings.active_embedder()
    timeout = common.EMBEDDING_TIMEOUT
    if ns.provider == "modal":
        url = ns.url or common.EMBEDDING_MODAL_URL
        if not url:
            return None, "no Modal URL: pass --url or set EMBEDDING_MODAL_URL"
        key = os.environ.get("MODAL_PROXY_KEY", "").strip()
        secret = os.environ.get("MODAL_PROXY_SECRET", "").strip()
        if not (key and secret):
            return None, "MODAL_PROXY_KEY / MODAL_PROXY_SECRET are not set"
        return (
            embeddings.ModalEmbedder(
                url,
                ns.model,
                ns.dim,
                key=key,
                secret=secret,
                gpu_rate=common.EMBEDDING_GPU_RATE,
                idle_s=common.EMBEDDING_MODAL_IDLE_SECONDS,
                timeout=timeout,
            ),
            None,
        )
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None, "OPENAI_API_KEY is not set"
    return OpenAIEmbedder(key, ns.model, ns.dim, timeout=timeout), None


class _Metered:
    """Wraps an embedder and sums the spend of its calls, failed ones included."""

    def __init__(self, inner):
        self.inner = inner
        self.provider = inner.provider
        self.space = inner.space
        self.usd = 0.0

    def embed(self, texts, *, kind):
        try:
            vectors, calls = self.inner.embed(texts, kind=kind)
        except EmbedError as exc:
            self.usd += sum(c.usd for c in exc.calls)
            raise
        self.usd += sum(c.usd for c in calls)
        return vectors, calls


# ---- synchronous path (Modal, or OpenAI without --openai-batch) ----


def _run_sync(conn, embedder, *, space, dim, max_items) -> int:
    metered = _Metered(embedder)
    total = rnd = 0
    while True:
        limit = ROUND_ITEMS if not max_items else min(ROUND_ITEMS, max_items - total)
        if limit <= 0:
            break
        rnd += 1
        before = metered.usd
        try:
            counts = grouping.embed_pending(
                conn,
                metered,
                space=space,
                dim=dim,
                limit=limit,
                purpose=PURPOSE,
            )
        except EmbedError as exc:
            conn.rollback()
            counts = getattr(exc, "counts", {}) or {}
            print(
                f"round {rnd}: embedded={counts.get('embedded', 0)}"
                f" reused={counts.get('reused', 0)} calls={counts.get('calls', 0)}"
                f" usd={metered.usd - before:.4f}"
            )
            print(f"provider error ({exc.kind}): {_scrub(exc)}")
            return EXIT_ERROR
        done = counts["embedded"] + counts["reused"] + counts["skipped"]
        if done == 0:
            break
        total += done
        print(
            f"round {rnd}: embedded={counts['embedded']} reused={counts['reused']}"
            f" skipped={counts['skipped']} calls={counts['calls']}"
            f" usd={metered.usd - before:.4f}"
        )
    print(f"embedding done: items={total} usd={metered.usd:.4f}")
    return EXIT_OK


# ---- OpenAI Batch API ----


def _http(call, url: str, what: str, **kw):
    try:
        resp = call(url, timeout=HTTP_TIMEOUT_S, **kw)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise Failure(EXIT_ERROR, f"openai {what}: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        body = _scrub(getattr(resp, "text", "") or "")
        raise Failure(EXIT_ERROR, f"openai {what}: HTTP {resp.status_code} {body}")
    return resp


def _json(resp, what: str):
    try:
        return resp.json()
    except ValueError as exc:
        raise Failure(EXIT_ERROR, f"openai {what}: reply is not JSON") from exc


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state.get("batches"), list):
            return state
    except (ValueError, OSError, AttributeError):
        pass
    raise Failure(EXIT_ERROR, f"{path} is unreadable: move it aside to start over")


def _select(conn, embedder, *, space, dim, max_items):
    """Items missing vectors, as {text hash: [item ids]} and {text hash: text},
    after skipping the empty and reusing vectors already known for the same text."""
    limit = max_items or 10**9
    items = gs.items_missing_vectors(conn, space=space, limit=limit)
    conn.commit()
    by_hash: dict[str, list[int]] = {}
    texts: dict[str, str] = {}
    skipped = 0
    for it in items:
        text = embed_text(it.title, it.body, it.url)
        if not text:
            gs.record_skip(conn, space=space, item_id=it.id, reason=grouping.EMPTY_TEXT)
            skipped += 1
            continue
        h = text_hash(text)
        texts.setdefault(h, text)
        by_hash.setdefault(h, []).append(it.id)
    conn.commit()
    known = gs.vectors_by_hash(conn, space=space, hashes=list(by_hash))
    rows = [(iid, h, known[h]) for h in by_hash if h in known for iid in by_hash[h]]
    gs.store_vectors(conn, space=space, dim=dim, rows=rows)
    conn.commit()
    todo = [h for h in by_hash if h not in known]
    print(
        f"selected: items={len(items)} unique_texts={len(todo)}"
        f" reused={len(rows)} skipped={skipped}"
    )
    return todo, texts, by_hash


def _submit(embedder, state_dir: Path, state: dict, todo, texts, by_hash, post) -> None:
    """Write the JSONL, upload and create each batch, saving state after each."""
    headers = {"Authorization": f"Bearer {embedder.api_key}"}
    chunk_n = max(1, BATCH_CHUNK)
    per_batch = max(1, BATCH_MAX_INPUTS // chunk_n)
    chunks = [todo[i : i + chunk_n] for i in range(0, len(todo), chunk_n)]
    state_path = state_dir / "state.json"
    for b_start in range(0, len(chunks), per_batch):
        group = chunks[b_start : b_start + per_batch]
        idx = len(state["batches"])
        lines, chunk_map, inputs = [], {}, 0
        for offset, hashes in enumerate(group):
            cid = f"chunk-{b_start + offset}"
            chunk_map[cid] = [[h, by_hash[h]] for h in hashes]
            inputs += len(hashes)
            lines.append(
                json.dumps(
                    {
                        "custom_id": cid,
                        "method": "POST",
                        "url": "/v1/embeddings",
                        "body": {
                            "model": embedder.model,
                            "input": [texts[h] for h in hashes],
                            "dimensions": embedder.dim,
                            "encoding_format": "float",
                        },
                    }
                )
            )
        path = state_dir / f"batch-{idx}.jsonl"
        data = ("\n".join(lines) + "\n").encode("utf-8")
        path.write_bytes(data)
        up = _http(
            post,
            f"{OPENAI_BASE}/files",
            "file upload",
            headers=headers,
            data={"purpose": "batch"},
            files={"file": (path.name, data)},
        )
        file_id = _json(up, "file upload")["id"]
        made = _http(
            post,
            f"{OPENAI_BASE}/batches",
            "batch create",
            headers=headers,
            json={
                "input_file_id": file_id,
                "endpoint": "/v1/embeddings",
                "completion_window": "24h",
            },
        )
        batch_id = _json(made, "batch create")["id"]
        state["batches"].append(
            {
                "id": batch_id,
                "input_file_id": file_id,
                "inputs": inputs,
                "done": False,
                "chunks": chunk_map,
            }
        )
        _save_state(state_path, state)
        print(f"submitted batch {batch_id}: chunks={len(group)} inputs={inputs}")


def _call(embedder, n: int, tokens: int, ok: bool) -> CallRecord:
    price = embeddings.OPENAI_PRICES_PER_MTOK[embedder.model]
    full = tokens * price / 1e6
    return CallRecord(
        "openai",
        embedder.space,
        n,
        tokens,
        None,
        None,
        None,
        True,
        full * embeddings.OPENAI_BATCH_FACTOR if ok else 0.0,
        full if ok else 0.0,
        "ok" if ok else "failed",
        None if ok else "batch line failed",
    )


def _collect(conn, embedder, batch: dict, text: str, *, space, dim) -> dict:
    """Store what a completed batch's output holds. Lines that failed (an error,
    a bad shape, a missing chunk) leave their items without vectors."""
    out = {"embedded": 0, "reused": 0, "failed": 0, "calls": 0, "usd": 0.0}
    rows, calls, seen = [], [], set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            cid = obj["custom_id"]
            chunk = batch["chunks"][cid]
        except (ValueError, KeyError, TypeError, AttributeError):
            out["failed"] += 1
            continue
        if cid in seen:
            continue
        seen.add(cid)
        resp = obj.get("response") or {}
        try:
            if obj.get("error") or resp.get("status_code") != 200:
                raise ValueError("line failed")
            body = resp["body"]
            data = sorted(body["data"], key=lambda d: d["index"])
            if [d["index"] for d in data] != list(range(len(chunk))):
                raise ValueError("indexes do not match the request")
            vecs = [embeddings._normalise(d["embedding"], dim) for d in data]
            tokens = int(body["usage"]["prompt_tokens"])
        except (KeyError, TypeError, ValueError, AttributeError):
            out["failed"] += 1
            calls.append(_call(embedder, len(chunk), 0, False))
            continue
        for (h, ids), vec in zip(chunk, vecs):
            lit = to_pg_vector(vec)
            rows.extend((iid, h, lit) for iid in ids)
            out["embedded"] += 1
            out["reused"] += len(ids) - 1
        calls.append(_call(embedder, len(chunk), tokens, True))
    out["failed"] += len(set(batch["chunks"]) - seen)
    gs.store_vectors(conn, space=space, dim=dim, rows=rows)
    gs.record_calls(conn, calls, purpose=PURPOSE)
    conn.commit()
    out["calls"] = len(calls)
    out["usd"] = sum(c.usd for c in calls)
    return out


def _lines(resp) -> int:
    return sum(1 for ln in (resp.text or "").splitlines() if ln.strip())


def _poll(conn, embedder, state_path, state, *, space, dim, get, sleep, wait) -> int:
    headers = {"Authorization": f"Bearer {embedder.api_key}"}
    waited = 0
    total = len(state["batches"])
    while True:
        for n, b in enumerate(state["batches"], 1):
            if b["done"]:
                continue
            info = _json(
                _http(
                    get,
                    f"{OPENAI_BASE}/batches/{b['id']}",
                    "batch status",
                    headers=headers,
                ),
                "batch status",
            )
            status = info.get("status")
            if status in BATCH_DEAD:
                err = info.get("error_file_id")
                n_err = 0
                if err:
                    n_err = _lines(
                        _http(
                            get,
                            f"{OPENAI_BASE}/files/{err}/content",
                            "error file",
                            headers=headers,
                        )
                    )
                print(
                    f"batch {b['id']} ({n}/{total}) status={status}"
                    f" error_lines={n_err}; {state_path} is kept: remove it to"
                    " resubmit what is still missing"
                )
                return EXIT_ERROR
            if status != "completed":
                continue
            out_id = info.get("output_file_id")
            if not out_id:
                raise Failure(
                    EXIT_ERROR, f"batch {b['id']} completed with no output file"
                )
            body = _http(
                get,
                f"{OPENAI_BASE}/files/{out_id}/content",
                "output file",
                headers=headers,
            )
            res = _collect(conn, embedder, b, body.text or "", space=space, dim=dim)
            b["done"] = True
            _save_state(state_path, state)
            print(
                f"batch {b['id']} ({n}/{total}) completed:"
                f" embedded={res['embedded']} reused={res['reused']}"
                f" failed_lines={res['failed']} calls={res['calls']}"
                f" usd={res['usd']:.4f}"
            )
        left = [b for b in state["batches"] if not b["done"]]
        if not left:
            return EXIT_OK
        if waited >= wait:
            print(f"in progress: {len(left)} of {total} batch(es); rerun later")
            return EXIT_IN_PROGRESS
        sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S


def _run_batch(conn, embedder, ns, *, space, dim, post, get, sleep) -> int:
    if embedder.model not in embeddings.OPENAI_PRICES_PER_MTOK:
        raise Failure(EXIT_ERROR, f"no price known for model {embedder.model!r}")
    state_dir = common.DATA_DIR / "embed_backfill" / space_slug(space)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = _load_state(state_path)
    if state is None or all(b["done"] for b in state["batches"]):
        todo, texts, by_hash = _select(
            conn, embedder, space=space, dim=dim, max_items=ns.max_items
        )
        if not todo:
            print("nothing to embed")
            return EXIT_OK
        tokens = sum(len(texts[h]) for h in todo) / 4
        price = embeddings.OPENAI_PRICES_PER_MTOK[embedder.model]
        est = tokens * price / 1e6 * embeddings.OPENAI_BATCH_FACTOR
        print(f"estimated_usd={est:.4f} (max {ns.max_usd:.2f})")
        if est > ns.max_usd:
            print("refused: the estimate is above --max-usd; nothing was submitted")
            return EXIT_USAGE
        state = {"space": space, "model": embedder.model, "dim": dim, "batches": []}
        _save_state(state_path, state)
        _submit(embedder, state_dir, state, todo, texts, by_hash, post)
    else:
        left = sum(1 for b in state["batches"] if not b["done"])
        print(f"resuming: {left} batch(es) already submitted")
    return _poll(
        conn,
        embedder,
        state_path,
        state,
        space=space,
        dim=dim,
        get=get,
        sleep=sleep,
        wait=ns.wait,
    )


# ---- grouping ----


def _group(conn, space: str) -> None:
    cal = gs.latest_calibration(conn, space)
    conn.commit()
    if cal is None:
        print(
            f"uncalibrated: {space} has no calibration; vectors stored, nothing grouped"
        )
        return
    th = grouping.Thresholds.from_calibration(cal)
    totals = {"grouped": 0, "opened": 0, "joined": 0}
    while True:
        res = grouping.assign_pending(
            conn,
            space=space,
            thresholds=th,
            window_hours=common.STORYLINE_GROUP_WINDOW_HOURS,
            limit=ASSIGN_ROUND,
        )
        for k in totals:
            totals[k] += res[k]
        if res["grouped"] == 0 or res["stopped_at_missing_vector"]:
            break
    note = (
        " (stopped at an item without a vector)"
        if res["stopped_at_missing_vector"]
        else ""
    )
    print(
        f"grouped={totals['grouped']} opened={totals['opened']}"
        f" joined={totals['joined']}{note}"
    )


# ---- entry point ----


def main(argv=None, *, embedder=None, post=None, get=None, sleep=time.sleep) -> int:
    try:
        ns = _parse(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    post = post or requests.post
    get = get or requests.get

    if embedder is None:
        embedder, problem = _build_embedder(ns)
        if embedder is None:
            print(f"no embedder: {_scrub(problem)}")
            return EXIT_ERROR
    if ns.openai_batch and embedder.provider != "openai":
        print("--openai-batch needs the openai provider")
        return EXIT_USAGE
    space = embedder.space
    dim = getattr(embedder, "dim", None) or ns.dim
    if not dim:
        print("the embedder has no dimension: pass --dim")
        return EXIT_USAGE
    if not db.is_configured():
        print("no database is configured")
        return EXIT_ERROR

    conn = db.connect()
    try:
        with grouping.space_lock(conn, space) as got:
            if not got:
                conn.rollback()
                print(f"busy: another pass holds {space}")
                return EXIT_BUSY
            conn.commit()
            try:
                if ns.openai_batch:
                    rc = _run_batch(
                        conn,
                        embedder,
                        ns,
                        space=space,
                        dim=dim,
                        post=post,
                        get=get,
                        sleep=sleep,
                    )
                else:
                    rc = _run_sync(
                        conn, embedder, space=space, dim=dim, max_items=ns.max_items
                    )
            except Failure as exc:
                conn.rollback()
                print(str(exc))
                return exc.code
            if rc == EXIT_OK and not ns.no_group:
                _group(conn, space)
            return rc
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
