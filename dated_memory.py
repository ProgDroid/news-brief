"""Dated memory for the brief (storylines phase 1, spec section 3).

Two jobs: give the brief a window of recent dated claims, and record which
evidence each day's brief added for each thesis key. This module owns ALL
`thesis_evidence` SQL; callers pass a connection and never write the SQL
themselves. There is no DELETE from that table, by design.
"""

from dataclasses import dataclass

DIRECTIONS = ("supports", "challenges")


@dataclass(frozen=True)
class EvidenceRow:
    thesis_key: str
    direction: str  # "supports" | "challenges"
    line: str


def save_brief_evidence(
    conn, on_date: str, rows: list[EvidenceRow], *, model: str
) -> int:
    """Insert brief-sourced rows dated `on_date` (YYYY-MM-DD); return the count written."""
    for r in rows:
        conn.execute(
            "INSERT INTO thesis_evidence (thesis_key, on_date, direction, line, source, model)"
            " VALUES (%s, %s, %s, %s, 'brief', %s)",
            (r.thesis_key, on_date, r.direction, r.line, model),
        )
    return len(rows)


def has_brief_evidence(conn, on_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM thesis_evidence WHERE on_date = %s AND source = 'brief' LIMIT 1",
        (on_date,),
    ).fetchone()
    return row is not None


def recent_evidence(conn, thesis_key: str, *, since: str) -> list[dict]:
    """Rows for one key with on_date >= since, newest first."""
    rows = conn.execute(
        "SELECT on_date, direction, line, source FROM thesis_evidence"
        " WHERE thesis_key = %s AND on_date >= %s ORDER BY on_date DESC, id DESC",
        (thesis_key, since),
    ).fetchall()
    return [
        {
            "on_date": d.isoformat(),
            "direction": direction,
            "line": line,
            "source": source,
        }
        for d, direction, line, source in rows
    ]


def keys_with_evidence(conn, *, since: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT thesis_key FROM thesis_evidence WHERE on_date >= %s ORDER BY thesis_key",
        (since,),
    ).fetchall()
    return [r[0] for r in rows]
