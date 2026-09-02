"""Postgres storage for the standing-claim ledger (news-brief-bqa.10).

The ONLY module that knows SQL, or that the ledger's `broke_on` is the column
`resolved_on`. It owns rows, marshalling and id allocation, and no business
logic: `merge_ledger` and `select_working_set` run unchanged on the dicts it
returns. Deliberately does NOT import brief_memory -- brief.py wires the two
together -- because the reverse edge would be a cycle.

Spec: docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md
"""

import json
from pathlib import Path

import db
from common import DATA_DIR, log

LEGACY_LEDGER_FILE = DATA_DIR / "brief_memory.json"
_IMPORT_LOCK = "claim_ledger_import"

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

    A KB-native claim (bqa.4b) has no ledger_id, and the ledger is not equipped
    to carry one: it would arrive with no `id`, take a render slot in
    select_working_set, be invisible to save_ledger's diff -- which keys on `id`
    -- and, if its last_reaffirmed were also NULL, take the whole feature down
    through _row_to_claim. This WHERE clause is what keeps the two populations
    apart until something is built to merge them.
    """
    columns = ", ".join(_KEY_TO_COLUMN.values())
    rows = conn.execute(
        f"SELECT {columns} FROM claims "
        f"WHERE retired_on IS NULL AND ledger_id IS NOT NULL "
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


def save_ledger(conn, before: dict, after: dict, today: str) -> tuple[int, int, int]:
    """Persist the merge's result. Returns (written, retired, skipped).

    `skipped` is the third number because the first two cannot tell a quiet run
    from a refused one: a row the schema rejects goes into log.exception and,
    without a counter, nowhere else, so "0 written" reads identically whether
    nothing changed or every row bounced. A fail-closed path has to say WHICH
    gate fired and with what numbers. It counts refusals on both loops -- a
    retirement that raises is exactly as invisible as a write that does.

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
    skipped = 0
    for cid, claim in after_by_id.items():
        if before_by_id.get(cid) == claim:
            continue
        try:
            with conn.transaction():
                _write_claim(conn, claim)
            written += 1
        except Exception:
            skipped += 1
            log.exception(f"Claim store: skipped an unwritable claim {cid!r}")

    retired = 0
    for cid in before_by_id.keys() - after_by_id.keys():
        try:
            with conn.transaction():
                # rowcount, not an unconditional +1: the UPDATE is guarded by
                # `retired_on IS NULL`, so a stale `before` -- one naming a row
                # another writer already retired, or one that is not there at
                # all -- matches nothing and the count would overstate what
                # happened. ledger_id is unique, so this is 0 or 1.
                cur = conn.execute(
                    "UPDATE claims SET retired_on = %s "
                    "WHERE ledger_id = %s AND retired_on IS NULL",
                    (today, cid),
                )
            if cur.rowcount:
                retired += 1
        except Exception:
            skipped += 1
            log.exception(f"Claim store: could not retire {cid!r}")
    conn.commit()
    return written, retired, skipped


def import_legacy(conn, path: Path | None = None) -> int:
    """Copy brief_memory.json into `claims`, once, while the table is empty.

    Same emptiness guard as the four sibling importers and for the same reason:
    it makes the import idempotent, so rollback means keeping the FILE rather
    than restoring a backup. The file is never written to and never deleted.

    The guard reads the TABLE, not the file: this ledger is a dict wrapping a
    claims list, so an empty list must not be read as "already imported".

    A malformed file imports nothing and logs -- it runs at boot and must not be
    able to stop one. Rows are validated by the schema and skipped individually,
    and the counts are compared afterwards so a silent per-row rejection shows
    up as a number rather than as nothing.
    """
    path = path or LEGACY_LEDGER_FILE
    with db.advisory_lock(conn, _IMPORT_LOCK) as got:
        if not got:
            return 0
        if conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, json.JSONDecodeError):
            log.exception(f"Could not read {path}; importing no claims")
            return 0
        claims = raw.get("claims") if isinstance(raw, dict) else None
        if not isinstance(claims, list):
            log.warning(f"{path} has no claims list; importing no claims")
            return 0

        # Pre-register before writing: what the file says should land.
        expected = [c for c in claims if isinstance(c, dict) and c.get("id")]
        for c in claims:
            if isinstance(c, dict) and not c.get("id"):
                # merge_ledger re-appends an id-less prior row, so one can
                # reach the file. It has no ledger_id to upsert against and
                # would otherwise vanish silently between two counts.
                log.warning(
                    "Claim import: skipped a row with no id: "
                    f"{str(c.get('claim'))[:80]!r}"
                )
        coverage = {}
        for c in expected:
            for key in c:
                coverage[key] = coverage.get(key, 0) + 1
        log.info(
            f"Claim import: {len(claims)} entries, {len(expected)} with an id; "
            f"key coverage {dict(sorted(coverage.items()))}"
        )

        imported = 0
        for entry in expected:
            row = dict(entry)
            # load_ledger treats a NULL last_reaffirmed as a hard error.
            row.setdefault("last_reaffirmed", row.get("first_seen"))
            status = row.get("status") or "standing"
            if status != "standing" and not row.get("broke_on"):
                # CHECK (status = 'standing' OR resolved_on IS NOT NULL). Rows
                # predating jx9.x's broke_on stamping would be rejected, and a
                # per-row skip is silent.
                row["broke_on"] = row["last_reaffirmed"]
                log.warning(
                    f"Claim import: {row['id']} is {status} with no broke_on; "
                    f"approximating resolved_on as {row['broke_on']}"
                )
            try:
                with conn.transaction():
                    _write_claim(conn, row)
                imported += 1
            except Exception:
                log.exception(f"Skipped an unimportable claim: {entry.get('id')!r}")
        conn.commit()

    landed = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
    if landed != len(expected):
        log.error(
            f"Claim import variance: expected {len(expected)} rows, {landed} landed. "
            "The difference was rejected individually -- see the skip lines above."
        )
    if imported:
        log.info(f"Imported {imported} claims from {path}")
    return imported


def degraded_block() -> str:
    """What the brief shows when the ledger could not be read.

    An empty string would delete the section, making an outage indistinguishable
    from an empty ledger. One line costs nothing and is the difference between a
    visible failure and an invisible one.
    """
    return (
        "## BACKGROUND ALREADY REPORTED\n"
        "Unavailable this run — the claim store could not be read, so treat "
        "nothing below as already established.\n"
    )
