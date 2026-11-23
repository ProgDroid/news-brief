"""Offline attach check (storylines spec 8.2, news-brief-al7.3).

Run once on the host over a past week of captured articles. Sonnet attaches
them twice (S1, S2) and Haiku once (H); pre-registered bars decide whether
Haiku may do the production attaching. It calls the PRODUCTION request builder
and validator, never a copy. On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/storylines_attach_check.py --week-ending YYYY-MM-DD [--sample 20]

Everything lives under DATA_DIR/storylines_check/<date>/: input.json (the
snapshot, written once and reused so every run scores the same inputs) and
runs/<run>.jsonl (one line per finished chunk; a rerun resumes after it). The
export holds real headlines: it stays in the data folder, never in git.

Exit codes: 0 PASS, 1 FAIL or ILL-POSED, 2 usage or snapshot error, a
configuration error (401/403/404) that stopped the check, or chunks still
pending after the runs (rerun to finish; no verdict is printed).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import comprehend  # noqa: E402
import common  # noqa: E402
import db  # noqa: E402
import storyline_store  # noqa: E402
import storylines  # noqa: E402

TRANSIENT_ATTEMPTS = 3
TRANSIENT_SLEEP_S = 30
SPEND_STAGE = "attach_check"
SAMPLE_SEED = 0
RUNS = ("S1", "S2", "H")

_sleep = time.sleep  # replaced in tests


class ConfigStop(Exception):
    """A configuration error (bad key, unknown model): stop the whole check."""


def _default_post(payload: dict) -> dict:
    return brief._post_messages(
        payload, timeout=brief.ATTACH_TIMEOUT, max_attempts=1, label="AttachCheck"
    )


def _storyline_to_json(s: storyline_store.Storyline) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "status": s.status,
        "pinned": s.pinned,
        "last_attached_at": s.last_attached_at.isoformat()
        if s.last_attached_at
        else None,
    }


def _storyline_from_json(d: dict) -> storyline_store.Storyline:
    last = d["last_attached_at"]
    return storyline_store.Storyline(
        id=d["id"],
        name=d["name"],
        description=d["description"],
        status=d["status"],
        pinned=d["pinned"],
        last_attached_at=datetime.fromisoformat(last) if last else None,
    )


def _load_or_write_snapshot(
    path: Path, week_ending: datetime
) -> tuple[list[dict], list[storyline_store.Storyline]] | None:
    """The snapshot, written on the first run. None when there is nothing to check."""
    if path.exists():
        snap = json.loads(path.read_text(encoding="utf-8"))
    else:
        with db.connect() as conn:
            items = storyline_store.items_between(
                conn, start=week_ending - timedelta(days=7), end=week_ending
            )
            live = storyline_store.live_storylines(conn)
        if not items or not live:
            print(
                f"snapshot: items={len(items)} storylines={len(live)}, nothing to check"
            )
            return None
        snap = {
            "week_ending": week_ending.date().isoformat(),
            "items": items,
            "storylines": [_storyline_to_json(s) for s in live],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves a half-written snapshot
    return snap["items"], [_storyline_from_json(d) for d in snap["storylines"]]


def _record_spend(model: str, resp: dict) -> None:
    with db.connect() as conn:
        comprehend.record_spend(conn, SPEND_STAGE, model, resp.get("usage") or {})
        conn.commit()


def _read_run(path: Path) -> dict[int, dict]:
    """chunk index -> its record, for the chunks already finished."""
    if not path.exists():
        return {}
    done = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            done[rec["chunk"]] = rec
        except (ValueError, KeyError, TypeError):
            continue  # a line cut short by a crash: that chunk is redone
    return done


def _post_with_retries(post, payload: dict) -> dict:
    """The response, or the last exception when it is transient and out of
    attempts. A non-transient error is raised at once."""
    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return post(payload)
        except Exception as exc:
            if (
                storylines.classify_error(exc) != "transient"
                or attempt == TRANSIENT_ATTEMPTS
            ):
                raise
            _sleep(TRANSIENT_SLEEP_S)
    raise AssertionError("unreachable")


def _attach_chunk(
    post, live: list[storyline_store.Storyline], chunk: list[dict], model: str
) -> tuple[dict, dict] | None:
    """(decided, problems) for one chunk, with ids as str keys, or None when the
    chunk stopped on a transient error and is left to a rerun."""
    everything = {str(a["id"]): "chunk failed" for a in chunk}
    payload, s_labels, a_labels = storylines.build_attach_request(
        live, chunk, model=model
    )
    try:
        resp = _post_with_retries(post, payload)
    except Exception as exc:
        if storylines.classify_error(exc) == "transient":
            return None
        if storylines.classify_error(exc) == "config":
            raise ConfigStop(f"{type(exc).__name__}: {exc}"[:200]) from exc
        return {}, {k: f"{type(exc).__name__}: {exc}"[:200] for k in everything}
    _record_spend(model, resp)
    result = storylines.validate_attach(resp, s_labels, a_labels)
    if result.whole is not None:
        return {}, {k: result.whole for k in everything}
    return (
        {str(k): v for k, v in result.decided.items()},
        {str(k): v for k, v in result.problems.items()},
    )


def _do_run(post, name: str, model: str, items, live, runs_dir: Path) -> None:
    path = runs_dir / f"{name}.jsonl"
    done = _read_run(path)
    size = storylines.ATTACH_BATCH
    chunks = [items[i : i + size] for i in range(0, len(items), size)]
    for index, chunk in enumerate(chunks):
        if index in done:
            continue
        out = _attach_chunk(post, live, chunk, model)
        if out is None:
            print(
                f"chunk {index} of {name} incomplete (transient errors): rerun to retry"
            )
            continue
        decided, problems = out
        runs_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            if path.stat().st_size and not path.read_bytes().endswith(b"\n"):
                f.write("\n")  # close a fragment left by a crash
            f.write(
                json.dumps({"chunk": index, "decided": decided, "problems": problems})
                + "\n"
            )
        print(f"{name}: chunk {index + 1}/{len(chunks)} done")


def _pending_chunks(items, runs_dir: Path) -> dict[str, int]:
    """run name -> chunks with no line in its jsonl (only runs with some)."""
    size = storylines.ATTACH_BATCH
    n_chunks = len(range(0, len(items), size))
    pending = {}
    for name in RUNS:
        missing = n_chunks - len(
            [i for i in _read_run(runs_dir / f"{name}.jsonl") if 0 <= i < n_chunks]
        )
        if missing:
            pending[name] = missing
    return pending


def _decided(path: Path) -> dict[int, set[int]]:
    run: dict[int, set[int]] = {}
    for rec in _read_run(path).values():
        for k, v in rec["decided"].items():
            run[int(k)] = set(v)
    return run


def _fmt(sids, names: dict[int, str]) -> str:
    return "[" + ", ".join(names.get(i, f"#{i}") for i in sorted(sids)) + "]"


def _score(items, live, runs_dir: Path, sample: int) -> int:
    runs = {n: _decided(runs_dir / f"{n}.jsonl") for n in RUNS}
    all_ids = {a["id"] for a in items}
    excluded = {n: all_ids - runs[n].keys() for n in RUNS}
    total = set().union(*excluded.values())
    print(
        "excluded: "
        + " ".join(f"{n}={len(excluded[n])}" for n in RUNS)
        + f" total={len(total)}"
    )
    n_all = len(all_ids)
    if len(excluded["H"]) / n_all > len(excluded["S1"]) / n_all + 0.02:
        print(
            "WARNING: H failed share exceeds S1's by more than 2 points; "
            "read the excluded items before accepting a PASS"
        )
    common_ids = all_ids - total
    s1, s2, h = ({i: runs[n][i] for i in common_ids} for n in ("S1", "S2", "H"))
    j_ss, n_ss = storylines.jaccard_mean(s1, s2)
    j_hs, n_hs = storylines.jaccard_mean(h, s1)
    rates = {n: storylines.attach_rate(r) for n, r in zip(RUNS, (s1, s2, h))}
    print(f"scored items={len(common_ids)}")
    print(f"J(S1,S2)={j_ss:.2f} (n={n_ss})  J(H,S1)={j_hs:.2f} (n={n_hs})")
    print("attach rate: " + " ".join(f"{n}={rates[n]:.2f}" for n in RUNS))
    kinds = storylines.differ_kinds(h, s1)
    print(
        f"differ: less={kinds['less']} more={kinds['more']} "
        f"elsewhere={kinds['elsewhere']}"
    )
    verdict, reasons = storylines.check_verdict(j_ss, j_hs, rates["H"], rates["S1"])
    print(f"verdict: {verdict}")
    for r in reasons:
        print(f"  {r}")
    # Terminal only: real headlines, never written to a file.
    titles = {a["id"]: a["title"] for a in items}
    names = {s.id: s.name for s in live}
    disagree = sorted(i for i in common_ids if h[i] != s1[i])
    for i in random.Random(SAMPLE_SEED).sample(disagree, min(sample, len(disagree))):
        print(f"sample: {titles[i]} | H={_fmt(h[i], names)} S1={_fmt(s1[i], names)}")
    return 0 if verdict == "PASS" else 1


def _parse_date(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None, *, post=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--week-ending", required=True, type=_parse_date)
    parser.add_argument("--sample", type=int, default=20)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    post = post or _default_post
    update_model = common.STORYLINE_UPDATE_MODEL
    attach_model = common.STORYLINE_ATTACH_MODEL
    for model in (update_model, attach_model):
        try:
            comprehend.price_of(model)
        except comprehend.UnpricedModel:
            print(f"model {model} has no price: add it to PRICES_PER_MTOK first")
            return 2
    day = args.week_ending.date().isoformat()
    root = common.DATA_DIR / "storylines_check" / day
    snap = _load_or_write_snapshot(root / "input.json", args.week_ending)
    if snap is None:
        return 2
    items, live = snap
    print(f"items={len(items)} storylines={len(live)}")
    for name, model in (
        ("S1", update_model),
        ("S2", update_model),
        ("H", attach_model),
    ):
        try:
            _do_run(post, name, model, items, live, root / "runs")
        except ConfigStop as stop:
            print(
                f"config error, check stopped (nothing written for the chunk): {stop}"
            )
            return 2
    pending = _pending_chunks(items, root / "runs")
    if pending:
        detail = ", ".join(f"{n}={pending.get(n, 0)}" for n in RUNS)
        print(
            f"INCOMPLETE: {sum(pending.values())} chunks pending ({detail}); "
            "rerun to finish"
        )
        return 2
    return _score(items, live, root / "runs", args.sample)


if __name__ == "__main__":
    sys.exit(main())
