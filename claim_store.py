"""Postgres storage for the standing-claim ledger (news-brief-bqa.10).

The ONLY module that knows SQL, or that the ledger's `broke_on` is the column
`resolved_on`. It owns rows, marshalling and id allocation, and no business
logic: `merge_ledger` and `select_working_set` run unchanged on the dicts it
returns. Deliberately does NOT import brief_memory -- brief.py wires the two
together -- because the reverse edge would be a cycle.

Spec: docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md
"""

from common import log

# Ledger key -> claims column. Three renames (DDL spec 5.1); `kind` is absent on
# purpose, being an admission guard that is never stored.
_KEY_TO_COLUMN = {
    "id": "ledger_id",
    "claim": "claim",
    "topic": "topic",
    "first_seen": "first_seen",
    "last_reaffirmed": "last_reaffirmed",
    "restate_count": "restate_count",
    "source_count": "source_count",
    "severity": "severity",
    "origin": "origin",
    "driver": "driver",
    "horizon_days": "horizon_days",
    "resolution_date": "resolution_date",
    "horizon_elapsed": "horizon_elapsed",
    "status": "status",
    "broke_on": "resolved_on",
    "broken_by": "broken_by_note",
    "extractor_model": "extractor_model",
    "prompt_version": "prompt_version",
}
_DATE_KEYS = ("first_seen", "last_reaffirmed", "resolution_date", "broke_on")

# Mirrors brief_memory._SEVERITY_RANK, which cannot be imported without a cycle.
# ELSE 1 reproduces _severity_rank's "unknown/missing -> normal".
_SEVERITY_ORDER_SQL = "CASE severity WHEN 'high' THEN 2 WHEN 'low' THEN 0 ELSE 1 END"


def _row_to_claim(row) -> dict:
    """One database row as the dict merge_ledger expects.

    A NULL column is OMITTED rather than carried as None, because that is what
    the JSON ledger did: a key it never set was simply absent, and the coercion
    helpers are all written for a missing key, not a null one.
    """
    claim = {}
    for key, value in zip(_KEY_TO_COLUMN, row):
        if value is None:
            continue
        claim[key] = value.strftime("%Y-%m-%d") if key in _DATE_KEYS else value
    if "last_reaffirmed" not in claim:
        raise ValueError(
            f"claim {claim.get('id')!r} has a NULL last_reaffirmed. merge_ledger "
            "indexes that key directly and select_working_set compares it to a "
            "string, so a null reaches a sort as None and raises TypeError far "
            "from here. The column is nullable for KB-native rows; ledger rows "
            "may not use that latitude (news-brief-bqa.10 spec 3.4)."
        )
    return claim


def load_ledger(conn) -> dict:
    """Every live claim, in the dict shape brief_memory reads.

    ORDER BY is not decoration. Both merge_ledger's and select_working_set's
    sorts are STABLE and use reverse=True, which does not reverse equal
    elements -- so ties break by input order. Reproducing the order
    merge_ledger last wrote keeps that deterministic instead of leaving it to
    the planner.
    """
    columns = ", ".join(_KEY_TO_COLUMN.values())
    rows = conn.execute(
        f"SELECT {columns} FROM claims WHERE retired_on IS NULL "
        f"ORDER BY {_SEVERITY_ORDER_SQL} DESC, last_reaffirmed DESC, ledger_id"
    ).fetchall()
    return {"version": 1, "claims": [_row_to_claim(r) for r in rows]}


def next_ledger_num(conn) -> int:
    """The next `c-NNNN` number, counted across ALL rows including retired ones.

    There is no WHERE clause on purpose. load_ledger hides retired rows, so an
    id computed from what it returns would reissue a retired claim's ledger_id,
    and the upsert would then resolve ON CONFLICT to that row and overwrite it.
    COALESCE covers an empty table and one holding only KB-native rows, where
    MAX() over a nullable TEXT column returns NULL.
    """
    return conn.execute(
        "SELECT COALESCE(MAX(SUBSTRING(ledger_id FROM 'c-(\\d+)')::int), 0) + 1 "
        "FROM claims"
    ).fetchone()[0]


def _upsert_sql(keys) -> str:
    """An INSERT naming exactly the columns this claim actually has.

    NOT a fixed eighteen-column statement with NULLs for the absent keys. An
    explicit NULL OVERRIDES a column DEFAULT and then violates NOT NULL -- it
    does not fall back to the default. `status`, `origin`, `severity` and
    `restate_count` are all NOT NULL DEFAULT on `claims`, and the measured
    legacy rows carry none of the first two (spec 5.1), so the fixed form would
    raise 23502 on every row of the real ledger and the per-row `except` would
    swallow all of it into a silent zero.

    Omitting a column from the UPDATE branch leaves its stored value alone,
    which is correct here: merge_ledger never deletes a key from a row (it
    copies with `dict(...)` and only ever sets), so a key absent from `after`
    was absent from `before` too.
    """
    cols = [_KEY_TO_COLUMN[k] for k in keys]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "ledger_id")
    conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return (
        f"INSERT INTO claims ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (ledger_id) {conflict}"
    )


def _write_claim(conn, claim: dict) -> None:
    """Upsert one claim. Dates go back as `%Y-%m-%d` strings; psycopg casts
    them to DATE."""
    keys = [k for k in _KEY_TO_COLUMN if k in claim]
    conn.execute(_upsert_sql(keys), tuple(claim[k] for k in keys))


def save_ledger(conn, before: dict, after: dict, today: str) -> tuple[int, int]:
    """Persist the merge's result. Returns (rows written, rows retired).

    `before` is the ledger as loaded; `after` is what merge_ledger returned.
    A row present in `before` and absent from `after` was dropped by the TTL --
    the only way a row leaves merge_ledger -- so it is RETIRED rather than
    deleted, which keeps claim_evidence, thesis_claims and story_members intact
    (spec 2.1).

    Each row gets its own transaction, following config.py:605-613: one row the
    schema rejects must not cost the operator the rest of the day's claims.
    """
    before_by_id = {c["id"]: c for c in before.get("claims", []) if c.get("id")}
    # Last write wins: merge_ledger can emit one id twice (see :294).
    after_by_id = {c["id"]: c for c in after.get("claims", []) if c.get("id")}

    written = 0
    for cid, claim in after_by_id.items():
        if before_by_id.get(cid) == claim:
            continue
        try:
            with conn.transaction():
                _write_claim(conn, claim)
            written += 1
        except Exception:
            log.exception(f"Claim store: skipped an unwritable claim {cid!r}")

    retired = 0
    for cid in before_by_id.keys() - after_by_id.keys():
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE claims SET retired_on = %s "
                    "WHERE ledger_id = %s AND retired_on IS NULL",
                    (today, cid),
                )
            retired += 1
        except Exception:
            log.exception(f"Claim store: could not retire {cid!r}")
    conn.commit()
    return written, retired
