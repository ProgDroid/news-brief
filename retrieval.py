"""Hybrid group retrieval (storylines plan 1): a vector arm and a trigram arm per
query, fused by reciprocal rank, every group read as it stood at `as_of`.

SQL is in `group_store`. A provider fault never raises from `search_groups`: it
degrades to the trigram arm and says why. A database error does raise.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import embeddings
import group_store as gs
from embeddings import Embedder, EmbedError, text_hash, to_pg_vector

RRF_K = 60
LEXICAL_MIN = 0.5  # provisional: Task 8 reports the lexical arm's precision
PER_QUERY_LIMIT = 50


@dataclass(frozen=True)
class GroupHit:
    group: gs.GroupView
    score: float
    best_similarity: float | None
    best_title_score: float | None


@dataclass(frozen=True)
class SearchResult:
    hits: list[GroupHit]
    degraded: bool
    reason: str | None


def rrf(rank_lists: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: best first, ties to the lower id."""
    scores: dict[int, float] = {}
    for ranked in rank_lists:
        for rank, gid in enumerate(ranked, start=1):
            scores[gid] = scores.get(gid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _query_key(text: str) -> str:
    # Read at call time: a new prompt version must miss every cached vector.
    return text_hash(f"query/v{embeddings.EMBED_PROMPT_VERSION}:{text}")


def query_vectors(
    conn, embedder: Embedder, *, space: str, dim: int, queries: Sequence[str]
) -> list[str]:
    """A vector literal per query, from the cache where known. Misses go to the
    provider in one call, kind="query"; its calls are recorded (purpose
    "queries") and committed before the vectors are used. Raises EmbedError."""
    keys = [_query_key(q) for q in queries]
    found: dict[str, str] = {}
    for key in keys:
        if key not in found:
            vec = gs.query_vector(conn, space=space, text_hash=key)
            if vec is not None:
                found[key] = vec
    misses = {k: q for k, q in zip(keys, queries) if k not in found}
    if misses:
        if embedder.space != space:
            raise EmbedError(
                "config", f"embedder is for space {embedder.space!r}, not {space!r}"
            )
        # End the read transaction before the HTTP call.
        conn.commit()
        try:
            vectors, calls = embedder.embed(list(misses.values()), kind="query")
        except EmbedError as e:
            gs.record_calls(conn, e.calls, purpose="queries")
            conn.commit()
            raise
        gs.record_calls(conn, calls, purpose="queries")
        conn.commit()
        if len(vectors) != len(misses):
            raise EmbedError(
                "bad_response", f"{len(vectors)} vectors for {len(misses)} queries"
            )
        for key, vec in zip(misses, vectors):
            if len(vec) != dim:
                raise EmbedError(
                    "bad_response", f"vector has {len(vec)} components, expected {dim}"
                )
            lit = to_pg_vector(vec)
            gs.store_query_vector(conn, space=space, dim=dim, text_hash=key, vector=lit)
            found[key] = lit
        conn.commit()
    return [found[k] for k in keys]


def search_groups(
    conn,
    embedder: Embedder | None,
    *,
    space: str,
    dim: int,
    queries: Sequence[str],
    since: datetime,
    as_of: datetime,
    limit: int = 60,
    min_similarity: float | None = None,
) -> SearchResult:
    """Groups matching any query, seen through members with
    `since < joined_at <= as_of`, best fused rank first.

    `min_similarity` defaults to the space's calibrated `retrieval_min_sim`; while
    that is NULL the vector arm has no floor and only rank counts, which is not a
    degraded result. A provider fault (or no embedder) gives the trigram arm
    alone with `degraded=True` and the reason."""
    if min_similarity is None:
        cal = gs.latest_calibration(conn, space)
        min_similarity = cal.retrieval_min_sim if cal else None

    vectors: list[str] | None = None
    degraded, reason = False, None
    if embedder is None:
        degraded, reason = True, "no embedder: trigram search only"
    else:
        try:
            vectors = query_vectors(
                conn, embedder, space=space, dim=dim, queries=queries
            )
        except EmbedError as e:
            degraded, reason = True, f"embedding failed ({e.kind}): {e}"

    rank_lists: list[list[int]] = []
    best_sim: dict[int, float] = {}
    best_title: dict[int, float] = {}
    for i, query in enumerate(queries):
        if vectors is not None:
            hits = gs.vector_hits(
                conn,
                space=space,
                vector=vectors[i],
                since=since,
                as_of=as_of,
                min_similarity=min_similarity,
                limit=PER_QUERY_LIMIT,
            )
            rank_lists.append([gid for gid, _ in hits])
            for gid, sim in hits:
                best_sim[gid] = max(sim, best_sim.get(gid, sim))
        lex = gs.lexical_hits(
            conn,
            space=space,
            query=query,
            since=since,
            as_of=as_of,
            min_score=LEXICAL_MIN,
            limit=PER_QUERY_LIMIT,
        )
        rank_lists.append([gid for gid, _ in lex])
        for gid, score in lex:
            best_title[gid] = max(score, best_title.get(gid, score))

    fused = rrf(rank_lists)
    views = gs.groups_as_of(conn, group_ids=[g for g, _ in fused], as_of=as_of)
    hits_out = [
        GroupHit(views[gid], score, best_sim.get(gid), best_title.get(gid))
        for gid, score in fused
        if gid in views
    ][:limit]
    return SearchResult(hits_out, degraded, reason)
