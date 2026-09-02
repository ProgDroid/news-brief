"""Storage tests for the claim ledger cutover (news-brief-bqa.10).

DB-gated for the same reason tests/test_kb_schema.py is: a skip is not a pass,
and every assertion here is about what Postgres actually does.
"""

import pytest

import brief_memory
import claim_store
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing."""
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    """A fully migrated database, through 0007."""
    db.run_migrations(conn)
    conn.commit()
    return conn


def _indexdef(conn, name):
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


def test_claims_has_a_nullable_retired_on_date(store):
    row = store.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert row == ("date", "YES")


def test_rule_four_index_excludes_retired_claims(store):
    """0006:215-218 records the earlier version of this bug: a claim that should
    have left the index never did, and rule 4 re-fired on it every morning."""
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")


def test_a_live_index_supports_the_daily_load(store):
    assert "retired_on IS NULL" in _indexdef(store, "claims_live")


def test_0007_rolls_back_and_reapplies(store):
    """No down migration is trusted until it has been run. The index must come
    BACK on rollback, not merely be dropped -- 0006 owns it, so leaving it
    missing would corrupt the schema 0006 promises."""
    db.run_migrations(store, direction="down", steps=1)
    store.commit()
    cols = store.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert cols is None
    restored = _indexdef(store, "claims_open_resolution")
    assert restored is not None and "retired_on" not in restored
    db.run_migrations(store)
    store.commit()
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")


def _insert(conn, **overrides):
    """Insert one claim row, defaulting every column the caller does not name."""
    row = {
        "ledger_id": "c-0001",
        "claim": "a durable fact",
        "topic": "x",
        "first_seen": "2026-06-01",
        "last_reaffirmed": "2026-06-24",
        "restate_count": 1,
        "severity": "normal",
        "status": "standing",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"INSERT INTO claims ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()


def test_load_returns_the_ledger_dict_shape(store):
    _insert(store)
    assert claim_store.load_ledger(store) == {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "a durable fact",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
                "severity": "normal",
                "status": "standing",
                "origin": "extracted",
            }
        ],
    }


def test_load_omits_a_null_column_rather_than_carrying_None(store):
    """The JSON ledger simply lacks a key it never set. A None in its place
    would reach `_coerce_*` helpers and sort keys that never expected one."""
    _insert(store)
    claim = claim_store.load_ledger(store)["claims"][0]
    assert "driver" not in claim
    assert "broke_on" not in claim


def test_load_excludes_retired_claims(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002", retired_on="2026-06-20")
    ids = [c["id"] for c in claim_store.load_ledger(store)["claims"]]
    assert ids == ["c-0001"]


def test_load_maps_broke_on_from_resolved_on(store):
    _insert(
        store, status="broken", resolved_on="2026-06-22", broken_by_note="a reversal"
    )
    claim = claim_store.load_ledger(store)["claims"][0]
    assert claim["broke_on"] == "2026-06-22"
    assert claim["broken_by"] == "a reversal"


def test_load_orders_by_severity_then_recency_then_id(store):
    """merge_ledger and select_working_set both sort with reverse=True, and
    Python's sort is STABLE -- reverse=True does not reverse equal elements. So
    ties break by INPUT order. Today that is the order merge_ledger last wrote
    to the file; from a database it would be whatever the planner returned, and
    two claims tied on severity and date would swap places between runs."""
    _insert(store, ledger_id="c-0001", severity="normal", last_reaffirmed="2026-06-24")
    _insert(store, ledger_id="c-0002", severity="high", last_reaffirmed="2026-06-01")
    _insert(store, ledger_id="c-0003", severity="normal", last_reaffirmed="2026-06-25")
    _insert(store, ledger_id="c-0004", severity="normal", last_reaffirmed="2026-06-24")
    ids = [c["id"] for c in claim_store.load_ledger(store)["claims"]]
    assert ids == ["c-0002", "c-0003", "c-0001", "c-0004"]


def test_a_null_last_reaffirmed_is_a_hard_error(store):
    """The column is DATE NULL (0006:194), but merge_ledger indexes
    last_reaffirmed directly and select_working_set compares it against "".
    `c.get("last_reaffirmed", "")` returns None when the key is PRESENT AND
    NULL, and the sort then raises TypeError. "Sorts last" holds only for a
    MISSING key. Failing here names the row; failing there names a comparison."""
    _insert(store, last_reaffirmed=None)
    with pytest.raises(ValueError, match="c-0001"):
        claim_store.load_ledger(store)


def test_load_on_an_empty_table_is_an_empty_ledger(store):
    assert claim_store.load_ledger(store) == {"version": 1, "claims": []}


def test_the_sql_severity_order_matches_the_python_rank(store):
    """The store cannot import brief_memory without a cycle, so the ordering is
    duplicated. This test is what stops the duplicate drifting."""
    for name, rank in brief_memory._SEVERITY_RANK.items():
        got = store.execute(
            f"SELECT {claim_store._SEVERITY_ORDER_SQL} FROM (SELECT %s::text AS severity) t",
            (name,),
        ).fetchone()[0]
        assert got == rank, f"{name}: SQL says {got}, Python says {rank}"


def test_next_ledger_num_counts_retired_rows(store):
    """The collision this exists to prevent is SILENT. load_ledger hides retired
    rows, so _max_id_num(prior)+1 would reissue c-0050, and an upsert keyed
    ON CONFLICT (ledger_id) resolves to the retired row and OVERWRITES it --
    handing a brand-new claim the retired one's first_seen and history."""
    _insert(store, ledger_id="c-0049")
    _insert(store, ledger_id="c-0050", retired_on="2026-06-20")
    assert claim_store.next_ledger_num(store) == 51


def test_next_ledger_num_on_an_empty_table_is_one(store):
    """ledger_id is TEXT NULL (0006:159), so a bare MAX() returns NULL here and
    f"c-{None:04d}" raises. COALESCE is load-bearing, not decoration."""
    assert claim_store.next_ledger_num(store) == 1


def test_next_ledger_num_ignores_rows_without_a_ledger_id(store):
    """bqa.4b will write KB-native claims with no ledger_id at all."""
    _insert(store, ledger_id="c-0007")
    store.execute(
        "INSERT INTO claims (claim, first_seen, last_reaffirmed) "
        "VALUES ('kb-native', '2026-06-01', '2026-06-01')"
    )
    store.commit()
    assert claim_store.next_ledger_num(store) == 8
