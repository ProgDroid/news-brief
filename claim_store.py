"""Postgres storage for the standing-claim ledger (news-brief-bqa.10).

The ONLY module that knows SQL, or that the ledger's `broke_on` is the column
`resolved_on`. It owns rows, marshalling and id allocation, and no business
logic: `merge_ledger` and `select_working_set` run unchanged on the dicts it
returns. Deliberately does NOT import brief_memory -- brief.py wires the two
together -- because the reverse edge would be a cycle.

Spec: docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md
"""

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
