"""Embedding calibration (storylines plan 1, Task 8): compare candidate spaces on
one past week, fit each one's grouping thresholds, recommend one, and on
`--accept` write that space's calibration row.

On the host (one --gpu per Modal space, so the cheaper-GPU tie-break can apply):

    docker compose run --rm --entrypoint python newsbrief \\
        scripts/embed_calibrate.py --week-ending YYYY-MM-DD \\
        --space 'modal:<model>@<dim>=<url>' --gpu 'modal:<model>@<dim>=T4' \\
        --space 'modal:<model2>@<dim>=<url2>' --gpu 'modal:<model2>@<dim>=L4'

then rerun the same command with `--accept <space id>`: every step is reused from
its file, so the rerun makes no judge call. An `openai:<model>@<dim>` space needs
`--include-openai`. GPU rates per second (Modal pricing page, 2026-11-24): T4
0.000164, L4 0.000222, A10 0.000306; OpenAI counts as cheapest.

Steps, each resumable from DATA_DIR/embed_calibration/<week-ending>/: snapshot,
embed (holding each space's lock), 72 h neighbour pairs sampled per similarity
band, the judge's labels, band-weighted thresholds, generated queries and their
relevance judgments, the report and the pre-registered recommendation. The
files hold real headlines and queries: they stay in the data folder, never in
git. The terminal gets counts and metrics only.

Exit codes: 0 a recommendation (or an accepted row); 1 none recommended, a
refused --accept, or a provider or database error; 2 usage, nothing to
calibrate, or a judge configuration error; 3 judge work still pending, rerun;
4 busy, another pass holds a space.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import common  # noqa: E402  (path shim above must run first)
import comprehend  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import group_store as gs  # noqa: E402
import grouping  # noqa: E402
import retrieval  # noqa: E402
import storylines  # noqa: E402
from embeddings import EmbedError, clean_title, embed_text  # noqa: E402

WINDOW_HOURS = 72
NEIGHBOURS = 10  # nearest earlier items per item
BAND_LO, BAND_STEP, N_BANDS = 0.30, 0.05, 14  # 0.30 .. 1.00
GRID = [round(BAND_LO + BAND_STEP * i, 2) for i in range(N_BANDS)]  # 0.30 .. 0.95
TITLE_GRID = [round(0.05 * i, 2) for i in range(1, 20)]  # 0.05 .. 0.95
JOIN_PRECISION, PAIR_PRECISION = 0.95, 0.90
MIN_BAND_LABELLED = 5  # fewer labelled in-pool pairs: the band is thin
BAR_PRECISION, BAR_RECALL = 0.90, 0.60
NEAR_DUP, SITUATIONS, PER_OUTLET = 0.8, 20, 2
QUERY_STYLES = ("open_question", "actor_goal", "constraint")
RANK_K = 30
PAIR_CHUNK, QUERY_CHUNK, REL_CHUNK = 20, 7, 30
JUDGE_TEXT_MAX = 500
RESAMPLES, SEED = 1000, 0
GPU_RATES = {"T4": 0.000164, "L4": 0.000222, "A10": 0.000306}
SPEND_STAGE, PURPOSE = "calibrate", "calibration"
JUDGE_TIMEOUT, TRANSIENT_ATTEMPTS, TRANSIENT_SLEEP_S = 300, 3, 30
# Opus 5.5 cannot run with thinking off and refuses a forced tool_choice
# (news-brief-931): effort is the only control, and its default is medium.
JUDGE_EFFORT, JUDGE_MAX_TOKENS = "low", 16000
PAIR_TOOL, QUERY_TOOL, RELEVANCE_TOOL = "label_pairs", "write_queries", "judge_items"
PAIR_VALUES = ("same", "different", "unclear")
EXIT_OK, EXIT_NONE, EXIT_USAGE, EXIT_PENDING, EXIT_BUSY = 0, 1, 2, 3, 4

_sleep = time.sleep  # replaced in tests
_SPEC = re.compile(r"^(modal|openai):([^@=]+)@(\d+)(?:=(.+))?$")


class JudgeError(Exception):
    """A judge reply of the wrong shape, or naming a label it was not given."""


class ConfigStop(Exception):
    """A judge configuration error (bad key, unknown model): stop."""


# ---- pure metrics ----


def band_of(sim: float | None) -> int | None:
    """The 0.05 band from 0.30 a similarity falls in; None under 0.30."""
    if sim is None or sim < BAND_LO - 1e-9:
        return None
    return min(N_BANDS - 1, int((sim - BAND_LO) / BAND_STEP + 1e-9))


def band_weights(bands, natural: dict[int, int]) -> list[float]:
    """Per labelled pair: (the space's share of its natural 72 h pairs in the
    pair's band) / (labelled pairs in that band). Out of band: 0."""
    total = sum(natural.values())
    labelled = Counter(b for b in bands if b is not None)
    return [
        natural.get(b, 0) / total / labelled[b] if b is not None and total else 0.0
        for b in bands
    ]


def weighted_prf(pred, truth, w) -> tuple[float, float, float]:
    tp = sum(x for p, t, x in zip(pred, truth, w) if p and t)
    pp = sum(x for p, x in zip(pred, w) if p)
    ap = sum(x for t, x in zip(truth, w) if t)
    prec = tp / pp if pp else 0.0  # no joins at all: not a usable threshold
    rec = tp / ap if ap else 0.0
    return prec, rec, (2 * prec * rec / (prec + rec) if prec + rec else 0.0)


def auc(scores, truth) -> float:
    """Unweighted ROC AUC (ties count half); nan without both classes."""
    pos = [s for s, t in zip(scores, truth) if t]
    neg = [s for s, t in zip(scores, truth) if not t]
    if not pos or not neg:
        return math.nan
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def join_threshold(sims, truth, w) -> float | None:
    """The lowest grid threshold whose weighted precision for vector-alone joins
    reaches JOIN_PRECISION."""
    for t in GRID:
        if weighted_prf([s >= t for s in sims], truth, w)[0] >= JOIN_PRECISION:
            return t
    return None


def _rule(sims, tsims, join, pair, title) -> list[bool]:
    return [s >= join or (s >= pair and ts >= title) for s, ts in zip(sims, tsims)]


def grid_search(sims, tsims, truth, w, *, join: float):
    """(pair_sim, title_sim, P, R, F1) of the combined rule with the highest
    weighted F1 at weighted precision >= PAIR_PRECISION; pair_sim <= join. Ties
    go to higher precision, then the stricter pair_sim, then title_sim."""
    best, best_key = None, None
    for pair in (g for g in GRID if g <= join):
        for title in TITLE_GRID:
            p, r, f1 = weighted_prf(_rule(sims, tsims, join, pair, title), truth, w)
            if p < PAIR_PRECISION:
                continue
            key = (f1, p, pair, title)
            if best_key is None or key > best_key:
                best, best_key = (pair, title, p, r, f1), key
    return best


def meets_bar(p: float, r: float) -> bool:
    """The pre-registered bar."""
    return p >= BAR_PRECISION and r >= BAR_RECALL


@dataclass(frozen=True)
class SpaceScore:
    join_sim: float | None
    pair_sim: float | None
    title_sim: float | None
    precision: float
    recall: float
    f1: float
    auc: float
    labelled: int
    reliable: bool
    verdict: str
    thin: tuple[float, ...] = ()  # lower edges of the bands left out


def scoring_rows(pairs, labels: dict, space: str) -> tuple[list, int]:
    """(rows, unclear) for one space: (similarity, title similarity, same) for
    each labelled pair in THIS space's own neighbour pool, `unclear` left out
    and counted. Pairs drawn only from another space's pool are not part of
    this space's natural population, so its band weights would misprice them."""
    rows, unclear = [], 0
    for p in pairs:
        if space not in p["in_pool"]:
            continue
        label = labels[f"{p['a']}-{p['b']}"]
        if label == "unclear":
            unclear += 1
            continue
        if p["sims"][space] is not None:
            rows.append((p["sims"][space], p["title_sim"], label == "same"))
    return rows, unclear


def score_space(rows, natural: dict[int, int]) -> SpaceScore:
    """rows: (similarity in this space, title similarity, same event) per labelled
    pair, `unclear` already left out."""
    sims, tsims, truth = ([r[i] for r in rows] for i in range(3))
    bands = [band_of(s) for s in sims]
    counts = Counter(b for b in bands if b is not None)
    thin = {b for b, n in counts.items() if n < MIN_BAND_LABELLED}
    # A thin band is left out of every weighted sum, never weighted up.
    w = band_weights([None if b in thin else b for b in bands], natural)
    edges = tuple(GRID[b] for b in sorted(thin))
    a = auc(sims, truth)
    join = join_threshold(sims, truth, w)
    found = grid_search(sims, tsims, truth, w, join=join) if join else None
    if found is None:
        why = "grouping unreliable (no join threshold reaches weighted precision 0.95)"
        return SpaceScore(
            None, None, None, 0.0, 0.0, 0.0, a, len(rows), False, why, edges
        )
    pair, title, p, r, f1 = found
    ok = meets_bar(p, r)
    verdict = "ok" if ok else "grouping unreliable"
    return SpaceScore(join, pair, title, p, r, f1, a, len(rows), ok, verdict, edges)


def ndcg_at(rels, n_relevant: int, k: int = RANK_K) -> float:
    """Binary nDCG@k; the ideal ranking holds min(n_relevant, k) relevant items."""
    dcg = sum(1 / math.log2(i + 2) for i, r in enumerate(rels[:k]) if r)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(n_relevant, k)))
    return dcg / ideal if ideal else math.nan


def bootstrap_interval(diffs, *, resamples=RESAMPLES, seed=SEED):
    """Percentile 95% interval of the mean of `diffs`, resampled over queries."""
    rng, n = random.Random(seed), len(diffs)
    means = sorted(
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    return means[int(0.025 * resamples)], means[math.ceil(0.975 * resamples) - 1]


def gpu_costs(spaces, gpu_args) -> dict[str, float]:
    """Cost rank per space: OpenAI 0, a Modal space its GPU's rate, inf without
    --gpu. Raises ValueError on a bad --gpu."""
    given = {}
    for arg in gpu_args:
        space, _, gpu = arg.rpartition("=")
        if space not in spaces or gpu not in GPU_RATES:
            raise ValueError(
                f"--gpu {arg!r}: want SPACE=GPU, GPU one of {list(GPU_RATES)}"
            )
        given[space] = GPU_RATES[gpu]
    return {
        s: 0.0 if s.startswith("openai:") else given.get(s, math.inf) for s in spaces
    }


def recommend(reliable, ndcg, cost) -> tuple[str | None, list[str]]:
    """The pre-registered rule: drop unreliable spaces; take the best mean nDCG@30;
    prefer a cheaper one when the bootstrap interval of (best - it) holds 0."""
    ok = sorted(s for s, good in reliable.items() if good)
    if not ok:
        return None, ["no space passed the bar"]

    def mean(s):
        return sum(ndcg[s]) / len(ndcg[s]) if ndcg[s] else -math.inf

    best = max(ok, key=lambda s: (mean(s), -cost[s]))
    notes, cheaper = [], []
    for s in ok:
        if s == best:
            continue
        if cost[s] == math.inf:
            notes.append(f"{s}: no --gpu, so the cheaper-GPU tie-break could not apply")
            continue
        if cost[s] >= cost[best] or not ndcg[s]:
            continue
        lo, hi = bootstrap_interval([b - c for b, c in zip(ndcg[best], ndcg[s])])
        notes.append(f"{s}: nDCG best-minus-it 95% interval [{lo:.3f}, {hi:.3f}]")
        if lo <= 0 <= hi:
            cheaper.append(s)
    if cheaper:
        pick = min(cheaper, key=lambda s: (cost[s], -mean(s)))
        notes.append(f"{pick} is on a cheaper GPU and not shown worse than {best}")
        return pick, notes
    return best, notes


def sample_pairs(pools, target: int, *, seed: int = SEED) -> list[tuple[int, int]]:
    """Up to `target` distinct pairs, round-robin over the bands of the union of
    every space's pool (a pair is in each band some space puts it in)."""
    by_band: dict[int, set] = defaultdict(set)
    for pool in pools.values():
        for pair, sim in pool.items():
            if (b := band_of(sim)) is not None:
                by_band[b].add(pair)
    rng = random.Random(seed)
    queues = []
    for b in sorted(by_band):
        q = sorted(by_band[b])
        rng.shuffle(q)
        queues.append(q)
    picked: list = []
    seen: set = set()
    while len(picked) < target and any(queues):
        for q in queues:
            while q and q[-1] in seen:
                q.pop()
            if q and len(picked) < target:
                seen.add(q[-1])
                picked.append(q.pop())
    return picked


# ---- the judge ----


def parse_answers(resp, *, tool: str, key: str, labels: dict) -> dict:
    """label -> its answer entry. Every label exactly once, none other."""
    if not isinstance(resp, dict) or resp.get("stop_reason") != "tool_use":
        stop = resp.get("stop_reason") if isinstance(resp, dict) else None
        raise JudgeError(f"unusable response (stop_reason={stop})")
    content = resp.get("content")
    block = next(
        (
            b
            for b in (content if isinstance(content, list) else [])
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") == tool
        ),
        None,
    )
    answers = (block or {}).get("input", {}).get("answers") if block else None
    if not isinstance(answers, list):
        raise JudgeError(f"{tool} returned no answers list")
    out: dict = {}
    for entry in answers:
        lab = entry.get(key) if isinstance(entry, dict) else None
        if not isinstance(lab, str) or lab not in labels:
            raise JudgeError(f"unknown label {str(lab)[:20]!r}")
        if lab in out:
            raise JudgeError(f"label {lab} answered twice")
        out[lab] = entry
    if len(out) != len(labels):
        raise JudgeError(f"{len(labels) - len(out)} label(s) unanswered")
    return out


def parse_pair_labels(resp, labels: dict) -> dict:
    out = {}
    for lab, e in parse_answers(
        resp, tool=PAIR_TOOL, key="pair", labels=labels
    ).items():
        if e.get("label") not in PAIR_VALUES:
            raise JudgeError(f"pair {lab}: label not one of {PAIR_VALUES}")
        out[labels[lab]] = e["label"]
    return out


def parse_relevance(resp, labels: dict) -> dict:
    out = {}
    for lab, e in parse_answers(
        resp, tool=RELEVANCE_TOOL, key="article", labels=labels
    ).items():
        if not isinstance(e.get("relevant"), bool):
            raise JudgeError(f"article {lab}: relevant is not a boolean")
        out[labels[lab]] = e["relevant"]
    return out


def parse_queries(resp, labels: dict) -> dict:
    out = {}
    for lab, e in parse_answers(
        resp, tool=QUERY_TOOL, key="event", labels=labels
    ).items():
        qs = [e.get(f) for f in QUERY_STYLES]
        if not all(isinstance(q, str) and q.strip() for q in qs):
            raise JudgeError(f"event {lab}: a query is missing")
        out[labels[lab]] = [q.strip() for q in qs]
    return out


def _payload(
    model: str,
    name: str,
    desc: str,
    props: dict,
    system: str,
    user: str,
) -> dict:
    tool = {
        "name": name,
        "description": desc,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": props,
                        "required": list(props),
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["answers"],
            "additionalProperties": False,
        },
    }
    return {
        "model": model,
        # Room for the thinking as well as the answers: it counts against this.
        "max_tokens": JUDGE_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": JUDGE_EFFORT},
        "system": system,
        "tools": [tool],
        # auto, steered by the prompt's "Call the tool once": a reply without
        # the call is a JudgeError and its chunk stays pending for the rerun.
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": user}],
    }


_PAIR_SYSTEM = (
    "Each pair below (P1, P2, ...) holds two news articles, A and B. Label it "
    "'same' when both report the same event: one happening (one decision, one "
    "attack, one release of figures), not merely the same topic, country or "
    "company. Label it 'different' otherwise, and 'unclear' only when the text "
    "cannot tell. Answer every pair once, using only the labels shown. Call the "
    "tool once."
)
_QUERY_SYSTEM = (
    "Each event below (E1, E2, ...) is one news article and its near-duplicates "
    "from other outlets. For each, write three search queries of the kind an "
    "analyst's working notes on the story would hold: open_question, a question "
    "the story leaves open; actor_goal, an actor and what it wants; constraint, "
    "a constraint that limits what can happen next. One sentence each, under 25 "
    "words. Do not reuse the headline's distinctive words: describe the "
    "situation in your own. Answer every event once. Call the tool once."
)
_REL_SYSTEM = (
    "Below is a search query from an analyst's notes, and numbered news articles "
    "(D1, D2, ...). Mark an article relevant when it reports facts that bear on "
    "what the query asks about, not when it merely shares a word or a topic. "
    "Answer every article once, using only the labels shown. Call the tool once."
)


def _enum(labels) -> dict:
    return {"type": "string", "enum": list(labels)}


def pair_request(model, pairs, texts):
    labels = {f"P{i}": p for i, p in enumerate(pairs, 1)}
    user = "\n\n".join(
        f"{lab}\nA: {texts[a]}\nB: {texts[b]}" for lab, (a, b) in labels.items()
    )
    props = {"pair": _enum(labels), "label": _enum(PAIR_VALUES)}
    desc = "Record whether each pair reports the same event."
    return _payload(
        model,
        PAIR_TOOL,
        desc,
        props,
        _PAIR_SYSTEM,
        f"{user}\n\nCall {PAIR_TOOL} once.",
    ), labels


def query_request(model, situations, texts):
    """situations: [(index, {"seed", "own"})]; the seed's text first."""
    labels = {f"E{i}": idx for i, (idx, _) in enumerate(situations, 1)}
    blocks = []
    for lab, (_, sit) in zip(labels, situations):
        ids = [sit["seed"]] + [i for i in sit["own"] if i != sit["seed"]][:3]
        blocks.append(lab + "\n" + "\n".join(f"- {texts[i]}" for i in ids))
    props = {"event": _enum(labels), **{f: {"type": "string"} for f in QUERY_STYLES}}
    user = "\n\n".join(blocks) + f"\n\nCall {QUERY_TOOL} once."
    return _payload(
        model,
        QUERY_TOOL,
        "Record three queries per event.",
        props,
        _QUERY_SYSTEM,
        user,
    ), labels


def relevance_request(model, query, item_ids, texts):
    labels = {f"D{i}": iid for i, iid in enumerate(item_ids, 1)}
    body = "\n".join(f"{lab}: {texts[iid]}" for lab, iid in labels.items())
    user = f"QUERY: {query}\n\n{body}\n\nCall {RELEVANCE_TOOL} once."
    props = {"article": _enum(labels), "relevant": {"type": "boolean"}}
    return _payload(
        model,
        RELEVANCE_TOOL,
        "Record whether each article is relevant.",
        props,
        _REL_SYSTEM,
        user,
    ), labels


def _default_post(payload: dict) -> dict:
    import brief  # heavy; only the real judge needs it

    return brief._post_messages(
        payload, timeout=JUDGE_TIMEOUT, max_attempts=1, label="Calibrate"
    )


class Judge:
    """Posts with transient retries and records every reply's spend, committed on
    its own. Prints exception types and status codes only, never a body."""

    def __init__(self, conn, post, model):
        self.conn, self.post, self.model = conn, post, model
        self.calls, self.usd = 0, 0.0

    def ask(self, payload) -> tuple[dict | None, float]:
        for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
            try:
                resp = self.post(payload)
                break
            except Exception as exc:
                kind = storylines.classify_error(exc)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                what = f"{type(exc).__name__} (status {status})"
                if kind == "transient" and attempt < TRANSIENT_ATTEMPTS:
                    _sleep(TRANSIENT_SLEEP_S)
                    continue
                if kind == "config":
                    raise ConfigStop(what) from exc
                print(f"judge call failed ({kind}): {what}")
                return None, 0.0
        usage = resp.get("usage") if isinstance(resp, dict) else None
        usd = comprehend.record_spend(self.conn, SPEND_STAGE, self.model, usage or {})
        self.conn.commit()
        self.calls += 1
        self.usd += usd
        return resp, usd


# ---- files ----


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves half a file


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a line cut short by a crash: that chunk is redone
    return out


def _append(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        if path.stat().st_size and not path.read_bytes().endswith(b"\n"):
            f.write("\n")
        f.write(json.dumps(rec) + "\n")


# ---- spaces ----


@dataclass(frozen=True)
class Space:
    provider: str
    model: str
    dim: int
    url: str | None
    space: str


def parse_space(spec: str) -> Space:
    m = _SPEC.match(spec.strip())
    if not m or (m.group(1) == "modal") != (m.group(4) is not None):
        raise ValueError(
            f"--space wants modal:<model>@<dim>=<url> or openai:<model>@<dim>, got {spec.split('=')[0]!r}"
        )
    provider, model, dim, url = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    return Space(provider, model, dim, url, embeddings.space_id(provider, model, dim))


def _build_embedder(sp: Space):
    timeout = common.EMBEDDING_TIMEOUT
    if sp.provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        return embeddings.OpenAIEmbedder(key, sp.model, sp.dim, timeout=timeout)
    return embeddings.ModalEmbedder(
        sp.url,
        sp.model,
        sp.dim,
        key=os.environ.get("MODAL_PROXY_KEY", "").strip(),
        secret=os.environ.get("MODAL_PROXY_SECRET", "").strip(),
        gpu_rate=common.EMBEDDING_GPU_RATE,
        idle_s=common.EMBEDDING_MODAL_IDLE_SECONDS,
        timeout=timeout,
    )


def _first_clause(exc) -> str:
    # A provider message can echo input text or a URL: keep the part before it.
    return str(exc).split(":")[0][:80]


# ---- steps ----


def _snapshot(conn, path: Path, since, until) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["items"]
    rows = gs.items_in_window(conn, since=since, until=until)
    conn.commit()
    items = [
        {
            "id": it.id,
            "outlet": it.outlet,
            "created_at": it.created_at.isoformat(),
            "title": clean_title(it.title, it.url),
            "text": " ".join(embed_text(it.title, it.body, it.url).split())[
                :JUDGE_TEXT_MAX
            ],
        }
        for it in rows
    ]
    if items:
        _write_json(
            path,
            {"since": since.isoformat(), "until": until.isoformat(), "items": items},
        )
    return items


def _embed(conn, spaces, embedders, ids) -> int | None:
    for sp in spaces:
        with grouping.space_lock(conn, sp.space) as got:
            if not got:
                conn.rollback()
                print(f"busy: another pass holds {sp.space}")
                return EXIT_BUSY
            conn.commit()
            try:
                c = grouping.embed_pending(
                    conn,
                    embedders[sp.space],
                    space=sp.space,
                    dim=sp.dim,
                    limit=len(ids),
                    purpose=PURPOSE,
                    item_ids=ids,
                )
            except EmbedError as exc:
                conn.rollback()
                print(
                    f"{sp.space}: embedding failed ({exc.kind}): {_first_clause(exc)}"
                )
                return EXIT_NONE
        print(
            f"embedded {sp.space}: embedded={c['embedded']} reused={c['reused']} skipped={c['skipped']} calls={c['calls']}"
        )
    return None


def _pools(conn, spaces, items, since) -> dict[str, dict[tuple[int, int], float]]:
    """Per space: {(earlier, later) ids: similarity} over each item's nearest
    earlier neighbours within WINDOW_HOURS, inside the snapshot."""
    at = {it["id"]: datetime.fromisoformat(it["created_at"]) for it in items}
    ids = list(at)
    pools = {}
    for sp in spaces:
        pool = {}
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            vecs = gs.item_vectors(conn, space=sp.space, item_ids=batch)
            for iid in batch:
                if iid not in vecs:
                    continue  # skipped in this space
                hits = gs.nearest_items(
                    conn,
                    space=sp.space,
                    vector=vecs[iid],
                    since=max(since, at[iid] - timedelta(hours=WINDOW_HOURS)),
                    until=at[iid] + timedelta(microseconds=1),
                    limit=NEIGHBOURS + 5,
                    exclude_ids=[iid],
                )
                earlier = [
                    (j, s) for j, s in hits if j in at and (at[j], j) < (at[iid], iid)
                ]
                for j, s in earlier[:NEIGHBOURS]:
                    pool[(j, iid)] = s
            conn.commit()
        pools[sp.space] = pool
        print(f"neighbours {sp.space}: pairs={len(pool)}")
    return pools


def _pairs(conn, path: Path, meta, pools, target, items) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["meta"] != meta:
            raise ValueError(
                f"{path.name} was drawn for other --space/--pairs values: move the folder aside, or rerun with the same ones"
            )
        if any("in_pool" not in p for p in data["pairs"]):
            raise ValueError(
                f"{path} predates per-space pools (no in_pool): delete it to"
                " re-sample; labels already judged are reused by pair key"
            )
        return data
    picked = sample_pairs(pools, target)
    titles = {it["id"]: it["title"] for it in items}
    sims = {s: gs.pair_similarities(conn, space=s, pairs=picked) for s in pools}
    tsims = gs.trigram_similarities(conn, [(titles[a], titles[b]) for a, b in picked])
    conn.commit()
    data = {
        "meta": meta,
        "natural": {
            s: Counter(b for v in pool.values() if (b := band_of(v)) is not None)
            for s, pool in pools.items()
        },
        "pairs": [
            {
                "a": a,
                "b": b,
                "sims": {s: sims[s][n] for s in pools},
                "title_sim": tsims[n],
                # The spaces whose own neighbour pool holds the pair: each space
                # is scored on these only.
                "in_pool": [s for s in pools if (a, b) in pools[s]],
            }
            for n, (a, b) in enumerate(picked)
        ],
    }
    _write_json(path, data)
    return json.loads(json.dumps(data))  # the same shape a rerun reads


def _situations(path: Path, meta, pools, items) -> list[dict]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["meta"] != meta:
            raise ValueError(
                f"{path.name} was drawn for other --space values: move the folder aside"
            )
        return data["situations"]
    near: dict[int, set] = defaultdict(set)
    for pool in pools.values():
        for (a, b), s in pool.items():
            if s > NEAR_DUP:
                near[a].add(b)
                near[b].add(a)
    outlet = {it["id"]: it["outlet"] for it in items}
    cands = sorted(i for i in near if any(outlet[j] != outlet[i] for j in near[i]))
    random.Random(SEED).shuffle(cands)
    chosen, used, per_outlet = [], set(), Counter()
    for i in cands:
        own = {i} | near[i]
        if own & used or per_outlet[outlet[i]] >= PER_OUTLET:
            continue
        chosen.append({"seed": i, "own": sorted(own)})
        used |= own
        per_outlet[outlet[i]] += 1
        if len(chosen) == SITUATIONS:
            break
    _write_json(path, {"meta": meta, "situations": chosen})
    return chosen


def _own(sit: dict) -> set[int]:
    """A situation's own items: the seed and its near-duplicates. Never scored."""
    return set(sit["own"])


def _label_pairs(judge, path, pairs, texts) -> tuple[dict, int]:
    """Labels by pair key ("a-b"). A pair already labelled in any earlier run is
    reused, so a re-drawn pairs.json judges only its new pairs."""
    labels: dict[str, str] = {}
    for r in _read_jsonl(path):
        labels.update(r.get("labels") or {})
    todo = [(p["a"], p["b"]) for p in pairs if f"{p['a']}-{p['b']}" not in labels]
    pending = 0
    for c in range(0, len(todo), PAIR_CHUNK):
        payload, chunk = pair_request(judge.model, todo[c : c + PAIR_CHUNK], texts)
        resp, usd = judge.ask(payload)
        try:
            if resp is None:
                raise JudgeError("no reply")
            got = parse_pair_labels(resp, chunk)
        except JudgeError as exc:
            print(f"pair chunk {c // PAIR_CHUNK}: {exc}")
            pending += 1
            continue
        rec = {"labels": {f"{a}-{b}": v for (a, b), v in got.items()}, "usd": usd}
        _append(path, rec)
        labels.update(rec["labels"])
    return labels, pending


def _write_queries(judge, path, situations, texts) -> tuple[dict, int]:
    done = {r["chunk"]: r for r in _read_jsonl(path) if "chunk" in r}
    pending = 0
    for c in range(0, len(situations), QUERY_CHUNK):
        if c // QUERY_CHUNK in done:
            continue
        chunk = [
            (i, situations[i]) for i in range(c, min(c + QUERY_CHUNK, len(situations)))
        ]
        payload, labels = query_request(judge.model, chunk, texts)
        resp, usd = judge.ask(payload)
        try:
            if resp is None:
                raise JudgeError("no reply")
            got = parse_queries(resp, labels)
        except JudgeError as exc:
            print(f"query chunk {c // QUERY_CHUNK}: {exc}")
            pending += 1
            continue
        rec = {
            "chunk": c // QUERY_CHUNK,
            "queries": {str(k): v for k, v in got.items()},
            "usd": usd,
        }
        _append(path, rec)
        done[rec["chunk"]] = rec
    by_sit = {int(k): v for r in done.values() for k, v in r["queries"].items()}
    # Query index = situation * 3 + style, labelled Q1.. in the files.
    return {3 * s + j: q for s, qs in by_sit.items() for j, q in enumerate(qs)}, pending


def _rankings(conn, spaces, embedders, queries, situations, items, since, until):
    """Per space: query index -> the top RANK_K snapshot items, own items left out."""
    in_snap = {it["id"] for it in items}
    qis = sorted(queries)
    out = {}
    for sp in spaces:
        vecs = retrieval.query_vectors(
            conn,
            embedders[sp.space],
            space=sp.space,
            dim=sp.dim,
            queries=[queries[q] for q in qis],
        )
        out[sp.space] = {}
        for qi, vec in zip(qis, vecs):
            own = _own(situations[qi // 3])
            hits = gs.nearest_items(
                conn,
                space=sp.space,
                vector=vec,
                since=since,
                until=until,
                limit=RANK_K,
                exclude_ids=sorted(own),
            )
            out[sp.space][qi] = [i for i, _ in hits if i in in_snap]
        conn.commit()
    return out


def _lexical(conn, queries, situations, items) -> dict[int, list[int]]:
    """Query index -> snapshot items whose cleaned title scores >= LEXICAL_MIN by
    word_similarity inside the query (the lexical arm), best first."""
    out = {}
    for qi, q in queries.items():
        own = _own(situations[qi // 3])
        scores = gs.trigram_similarities(
            conn, [(it["title"], q) for it in items], word=True
        )
        hits = sorted(
            (-s, it["id"])
            for s, it in zip(scores, items)
            if s >= retrieval.LEXICAL_MIN and it["id"] not in own
        )
        out[qi] = [i for _, i in hits[:RANK_K]]
    conn.commit()
    return out


def _judge_relevance(judge, path, pools, queries, texts) -> tuple[dict, int]:
    judged: dict[int, dict[int, bool]] = defaultdict(dict)
    for r in _read_jsonl(path):
        if "query" in r:
            judged[r["query"]].update({int(k): v for k, v in r["judged"].items()})
    pending = 0
    for qi in sorted(pools):
        todo = sorted(pools[qi] - judged[qi].keys())
        for c in range(0, len(todo), REL_CHUNK):
            payload, labels = relevance_request(
                judge.model, queries[qi], todo[c : c + REL_CHUNK], texts
            )
            resp, usd = judge.ask(payload)
            try:
                if resp is None:
                    raise JudgeError("no reply")
                got = parse_relevance(resp, labels)
            except JudgeError as exc:
                print(f"relevance Q{qi + 1}: {exc}")
                pending += 1
                continue
            _append(
                path,
                {
                    "query": qi,
                    "judged": {str(k): v for k, v in got.items()},
                    "usd": usd,
                },
            )
            judged[qi].update(got)
    return judged, pending


def _retrieval_scores(rankings, pools, judged):
    """Per space: (per-query nDCG@30 list, mean recall@30), over the queries whose
    pool holds a relevant item, in one shared order."""
    qis = sorted(q for q in pools if any(judged[q].get(i) for i in pools[q]))
    out = {}
    for space, ranks in rankings.items():
        nd, rec = [], []
        for q in qis:
            n_rel = sum(1 for i in pools[q] if judged[q].get(i))
            rels = [bool(judged[q].get(i)) for i in ranks[q][:RANK_K]]
            nd.append(ndcg_at(rels, n_rel))
            rec.append(sum(rels) / n_rel)
        out[space] = (nd, sum(rec) / len(rec) if rec else math.nan)
    return out, len(qis)


# ---- --accept ----


def _setting(key: str, value) -> str:
    v = str(value).replace("'", "''")
    return (
        f"INSERT INTO settings (key, user_id, value) VALUES ('{key}', NULL, '{v}')"
        " ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE"
        " SET value = EXCLUDED.value, updated_at = now();"
    )


def _accept(
    conn, sp: Space, score: SpaceScore, ns, week_end, gpu: dict, root: Path
) -> int:
    if not score.reliable:
        print(f"--accept refused: {sp.space} is {score.verdict}")
        return EXIT_NONE
    gs.write_calibration(
        conn,
        gs.Calibration(
            space=sp.space,
            join_sim=score.join_sim,
            pair_sim=score.pair_sim,
            title_sim=score.title_sim,
            retrieval_min_sim=None,  # fitted in plan 2 on real document queries
            weighted_precision=score.precision,
            weighted_recall=score.recall,
            pairs_labelled=score.labelled,
            prompt_version=embeddings.EMBED_PROMPT_VERSION,
            judge_model=ns.judge_model,
            week_ending=week_end.date(),
        ),
    )
    conn.commit()
    print(f"accepted: calibration row written for {sp.space}")
    rows = [("EMBEDDING_PROVIDER", sp.provider), ("EMBEDDING_DIM", sp.dim)]
    if sp.provider == "openai":
        rows.insert(1, ("EMBEDDING_OPENAI_MODEL", sp.model))
    else:
        rows.insert(1, ("EMBEDDING_MODAL_MODEL", sp.model))
        rows.append(("EMBEDDING_MODAL_URL", sp.url))
        if gpu.get(sp.space) is not None:
            rows.append(("EMBEDDING_GPU_RATE", gpu[sp.space]))
    path = root / f"activate-{re.sub(r'[^A-Za-z0-9._-]', '_', sp.space)}.sql"
    path.write_text("\n".join(_setting(k, v) for k, v in rows) + "\n", encoding="utf-8")
    print("to make it active, run in psql:")
    for k, v in rows:
        if k == "EMBEDDING_MODAL_URL":
            print(f"-- {k}: the statement is in {path} (the URL is not printed)")
        else:
            print(_setting(k, v))
    if sp.provider == "modal" and gpu.get(sp.space) is None:
        print("-- EMBEDDING_GPU_RATE: pass --gpu for this space to print it")
    return EXIT_OK


# ---- entry point ----


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="embed_calibrate", description=__doc__.splitlines()[0]
    )
    p.add_argument(
        "--week-ending",
        required=True,
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc),
    )
    p.add_argument(
        "--space",
        action="append",
        required=True,
        help="modal:<model>@<dim>=<url> or openai:<model>@<dim>",
    )
    p.add_argument("--pairs", type=int, default=600)
    p.add_argument("--judge-model", default="claude-opus-5-5")
    p.add_argument("--accept", metavar="SPACE")
    p.add_argument("--include-openai", action="store_true")
    p.add_argument(
        "--gpu",
        action="append",
        default=[],
        metavar="SPACE=GPU",
        help=f"one of {', '.join(GPU_RATES)}",
    )
    return p.parse_args(argv)


def main(argv=None, *, embedders=None, post=None) -> int:
    try:
        ns = _parse(sys.argv[1:] if argv is None else argv)
        spaces = [parse_space(s) for s in ns.space]
        ids_ = [sp.space for sp in spaces]
        if len(set(ids_)) != len(ids_):
            raise ValueError("a --space is given twice")
        gpu_cost = gpu_costs(ids_, ns.gpu)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except ValueError as exc:
        print(str(exc))
        return EXIT_USAGE
    if any(sp.provider == "openai" for sp in spaces):
        if not ns.include_openai:
            print(
                "an openai: space sends this week's text to OpenAI: pass --include-openai to allow it"
            )
            return EXIT_USAGE
        print(
            "--include-openai: this run sends one week of titles and summaries, and the generated queries, to OpenAI"
        )
    if ns.accept is not None and ns.accept not in ids_:
        print("--accept must name one of the --space ids (without the URL)")
        return EXIT_USAGE
    try:
        comprehend.price_of(ns.judge_model)
    except comprehend.UnpricedModel:
        print(f"model {ns.judge_model} has no price: add it to PRICES_PER_MTOK first")
        return EXIT_USAGE
    if not db.is_configured():
        print("no database is configured")
        return EXIT_NONE
    embedders = dict(embedders or {})
    for sp in spaces:
        embedders.setdefault(sp.space, _build_embedder(sp))

    week_end = ns.week_ending
    since = week_end - timedelta(days=7)
    root = common.DATA_DIR / "embed_calibration" / week_end.date().isoformat()
    conn = db.connect()
    try:
        items = _snapshot(conn, root / "snapshot.json", since, week_end)
        print(f"items={len(items)}")
        if not items:
            print("nothing to calibrate")
            return EXIT_USAGE
        rc = _embed(conn, spaces, embedders, [it["id"] for it in items])
        if rc is not None:
            return rc
        meta = {"spaces": sorted(ids_), "pairs": ns.pairs}
        try:
            pools = None
            if (
                not (root / "pairs.json").exists()
                or not (root / "situations.json").exists()
            ):
                pools = _pools(conn, spaces, items, since)
            data = _pairs(conn, root / "pairs.json", meta, pools, ns.pairs, items)
            situations = _situations(
                root / "situations.json", {"spaces": meta["spaces"]}, pools, items
            )
        except ValueError as exc:
            print(str(exc))
            return EXIT_USAGE
        texts = {it["id"]: it["text"] for it in items}
        judge = Judge(conn, post or _default_post, ns.judge_model)
        try:
            labels, p_pend = _label_pairs(
                judge, root / "labels.jsonl", data["pairs"], texts
            )
            queries, q_pend = _write_queries(
                judge, root / "queries.jsonl", situations, texts
            )
            try:
                ranks = _rankings(
                    conn, spaces, embedders, queries, situations, items, since, week_end
                )
            except EmbedError as exc:
                conn.rollback()
                print(f"query embedding failed ({exc.kind}): {_first_clause(exc)}")
                return EXIT_NONE
            lex = _lexical(conn, queries, situations, items)
            pools_q = {
                q: set(lex[q]).union(*(r[q] for r in ranks.values())) for q in queries
            }
            judged, r_pend = _judge_relevance(
                judge, root / "relevance.jsonl", pools_q, queries, texts
            )
        except ConfigStop as stop:
            print(f"judge configuration error, stopped: {stop}")
            return EXIT_USAGE
        print(
            f"judge: model={ns.judge_model} calls={judge.calls} usd={judge.usd:.4f} (this run)"
        )
        if p_pend or q_pend or r_pend:
            print(
                f"INCOMPLETE: pending pair_chunks={p_pend} query_chunks={q_pend} relevance_chunks={r_pend}; rerun to finish"
            )
            return EXIT_PENDING

        scores, unclear = {}, {}
        for sp in spaces:
            rows, unclear[sp.space] = scoring_rows(data["pairs"], labels, sp.space)
            natural = {int(b): n for b, n in data["natural"][sp.space].items()}
            scores[sp.space] = score_space(rows, natural)
        retr, n_q = _retrieval_scores(ranks, pools_q, judged)

        def f(x):
            return "none" if x is None else f"{x:.2f}"

        def thin(edges):
            return "|".join(f"{e:.2f}" for e in edges) or "none"

        print(
            f"each space is scored on its own pool's pairs; a band with fewer than"
            f" {MIN_BAND_LABELLED} of them is thin and left out of wP and wR"
        )

        for sp in spaces:
            s, (nd, rec) = scores[sp.space], retr[sp.space]
            mean_nd = sum(nd) / len(nd) if nd else math.nan
            print(
                f"space={sp.space} join={f(s.join_sim)} pair={f(s.pair_sim)} title={f(s.title_sim)}"
                f" wP={s.precision:.2f} wR={s.recall:.2f} wF1={s.f1:.2f} auc={s.auc:.2f}"
                f" labelled={s.labelled} unclear={unclear[sp.space]}"
                f" thin={thin(s.thin)} verdict={s.verdict}"
                f" ndcg30={mean_nd:.3f} recall30={rec:.3f} queries={n_q}"
            )
        lex_hits = [(q, i) for q, ids in lex.items() for i in ids]
        good = sum(1 for q, i in lex_hits if judged[q].get(i))
        prec = f"{good / len(lex_hits):.2f}" if lex_hits else "n/a"
        print(
            f"lexical: min={retrieval.LEXICAL_MIN} hits={len(lex_hits)} precision={prec}"
        )
        spent = sum(
            r.get("usd", 0.0)
            for n in ("labels", "queries", "relevance")
            for r in _read_jsonl(root / f"{n}.jsonl")
        )
        print(f"judge: usd={spent:.4f} (all runs, answered calls)")

        choice, notes = recommend(
            {s: v.reliable for s, v in scores.items()},
            {s: retr[s][0] for s in retr},
            gpu_cost,
        )
        print(f"recommendation: {choice or 'none'}")
        for n in notes:
            print(f"  note: {n}")
        if ns.accept is not None:
            sp = next(s for s in spaces if s.space == ns.accept)
            rate = {s: r for s, r in gpu_cost.items() if r not in (0.0, math.inf)}
            return _accept(conn, sp, scores[sp.space], ns, week_end, rate, root)
        return EXIT_OK if choice else EXIT_NONE
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
