"""Storage tests for the claim ledger cutover (news-brief-bqa.10).

DB-gated for the same reason tests/test_kb_schema.py is: a skip is not a pass,
and every assertion here is about what Postgres actually does.
"""

import json

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


_ALL_KEYS = {
    "id": "c-0001",
    "claim": "a durable fact",
    "topic": "energy",
    "first_seen": "2026-06-01",
    "last_reaffirmed": "2026-06-24",
    "restate_count": 3,
    "source_count": 5,
    "severity": "high",
    "origin": "extracted",
    "driver": "a mechanism",
    "horizon_days": 180,
    "resolution_date": "2026-12-01",
    "horizon_elapsed": 23,
    "status": "broken",
    "broke_on": "2026-06-22",
    "broken_by": "a reversal",
    "extractor_model": "claude-haiku-4-5-20251001",
    "prompt_version": 5,
}


def test_all_eighteen_ledger_keys_survive_a_round_trip(store):
    """DDL spec 5.1's full superset, both directions (obligation 2). A key that
    silently fails to persist is invisible until an audit needs it."""
    after = {"version": 1, "claims": [dict(_ALL_KEYS)]}
    claim_store.save_ledger(store, {"version": 1, "claims": []}, after, "2026-06-24")
    assert claim_store.load_ledger(store)["claims"][0] == _ALL_KEYS


def test_save_writes_only_the_rows_that_changed(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [dict(c) for c in before["claims"]]}
    after["claims"][0]["topic"] = "changed"
    written, retired, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (1, 0)


def test_a_row_the_merge_dropped_is_retired_not_deleted(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [before["claims"][0]]}
    written, retired, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (0, 1)
    row = store.execute(
        "SELECT retired_on FROM claims WHERE ledger_id = 'c-0002'"
    ).fetchone()
    assert row[0].strftime("%Y-%m-%d") == "2026-06-25"


def test_a_retired_row_leaves_the_ledger_but_not_the_table(store):
    _insert(store, ledger_id="c-0001")
    before = claim_store.load_ledger(store)
    claim_store.save_ledger(store, before, {"version": 1, "claims": []}, "2026-06-25")
    assert claim_store.load_ledger(store)["claims"] == []
    assert store.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_an_id_echoed_twice_in_one_reply_becomes_one_row(store):
    """merge_ledger:294 re-enters the `cid in by_id` branch without checking
    `returned`, so a reply echoing one id twice yields two dicts carrying it.
    Harmless in a JSON list; two upserts on one unique key here."""
    _insert(store, ledger_id="c-0001")
    before = claim_store.load_ledger(store)
    twin = dict(before["claims"][0])
    twin["topic"] = "second"
    after = {"version": 1, "claims": [before["claims"][0], twin]}
    written, retired, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (1, 0)
    assert claim_store.load_ledger(store)["claims"][0]["topic"] == "second"


def test_an_ordinary_reaffirm_does_not_trip_the_immutability_trigger(store):
    """claims_freeze_claim_text_trg (0006:274) is BEFORE UPDATE FOR EACH ROW, so
    ON CONFLICT DO UPDATE fires it on every reaffirm. save_ledger is the first
    UPDATE writer against this table; nothing had exercised that interaction."""
    _insert(store, ledger_id="c-0001", status="standing")
    before = claim_store.load_ledger(store)
    after = {
        "version": 1,
        "claims": [dict(before["claims"][0], last_reaffirmed="2026-06-25")],
    }
    written, _, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert written == 1


def test_rewriting_a_broken_claim_is_refused_by_the_trigger(store):
    """merge_ledger already refuses this (jx9.5), so the store should never send
    it. The trigger is defence in depth, and this records that the store's
    per-row transaction turns its RAISE into one skipped row rather than a lost
    day of claims."""
    _insert(
        store,
        ledger_id="c-0001",
        status="broken",
        resolved_on="2026-06-22",
        broken_by_note="a reversal",
    )
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [dict(before["claims"][0], claim="rewritten")]}
    written, _, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert written == 0
    assert claim_store.load_ledger(store)["claims"][0]["claim"] == "a durable fact"


def test_one_rejected_row_does_not_cost_the_others(store):
    """Per-row transactions, following config.py:605-613. A single transaction
    around the batch would discard every good row for one bad one."""
    good = dict(_ALL_KEYS)
    bad = dict(_ALL_KEYS, id="c-0002", status="broken", broke_on=None, broken_by=None)
    after = {"version": 1, "claims": [good, bad]}
    written, _, _ = claim_store.save_ledger(
        store, {"version": 1, "claims": []}, after, "2026-06-24"
    )
    assert written == 1
    assert [c["id"] for c in claim_store.load_ledger(store)["claims"]] == ["c-0001"]


def test_a_row_the_schema_refuses_is_counted_as_skipped(store):
    """(0, 0) says nothing about whether the day was quiet or the day bounced.
    A fail-closed path has to name the gate that fired, with a number."""
    bad = dict(_ALL_KEYS, status="broken", broke_on=None, broken_by=None)
    counts = claim_store.save_ledger(
        store,
        {"version": 1, "claims": []},
        {"version": 1, "claims": [bad]},
        "2026-06-24",
    )
    assert counts == (0, 0, 1)
    assert claim_store.load_ledger(store)["claims"] == []


def test_an_unattributed_break_survives_the_round_trip_as_broken(store):
    """The whole failure end to end: the model marks a claim broken and names
    nothing, the CHECK rejects the row, the per-row `except` swallows it, and
    the ledger reads back 'standing' forever. Nothing between merge_ledger and
    Postgres is stubbed here, because every layer in that chain reported
    success while the fact was being lost."""
    _insert(store, ledger_id="c-0001", status="standing")
    before = claim_store.load_ledger(store)
    after = brief_memory.merge_ledger(
        before,
        [{"id": "c-0001", "claim": "a durable fact", "status": "broken"}],
        "2026-06-25",
    )
    written, _, skipped = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, skipped) == (1, 0)
    stored = claim_store.load_ledger(store)["claims"][0]
    assert stored["status"] == "broken"
    assert stored["broken_by"] == brief_memory.UNATTRIBUTED_BREAK


def test_load_excludes_a_kb_native_claim_that_has_no_ledger_id(store):
    """bqa.4b writes claims with no ledger_id. One would arrive with no `id`:
    it takes a working-set slot, is invisible to save_ledger's diff, and -- as
    the second row here has -- a NULL last_reaffirmed then raises out of
    _row_to_claim and takes the whole feature down."""
    _insert(store, ledger_id="c-0001")
    store.execute(
        "INSERT INTO claims (claim, first_seen, last_reaffirmed) "
        "VALUES ('kb-native', '2026-06-01', '2026-06-02')"
    )
    store.execute(
        "INSERT INTO claims (claim, first_seen) VALUES ('kb-native, undated', '2026-06-01')"
    )
    store.commit()
    assert [c["id"] for c in claim_store.load_ledger(store)["claims"]] == ["c-0001"]


def test_retiring_a_row_that_is_already_retired_is_not_counted(store):
    """The UPDATE carries `AND retired_on IS NULL`, so a stale `before` matches
    zero rows. Counting it anyway reports a retirement that did not happen."""
    _insert(store, ledger_id="c-0001")
    before = claim_store.load_ledger(store)
    store.execute("UPDATE claims SET retired_on = '2026-06-24'")
    store.commit()
    counts = claim_store.save_ledger(
        store, before, {"version": 1, "claims": []}, "2026-06-25"
    )
    assert counts == (0, 0, 0)
    row = store.execute("SELECT retired_on FROM claims").fetchone()
    assert row[0].strftime("%Y-%m-%d") == "2026-06-24"


def _write_ledger(tmp_path, claims):
    p = tmp_path / "brief_memory.json"
    p.write_text(json.dumps({"version": 1, "claims": claims}), encoding="utf-8")
    return p


def test_import_accepts_the_eight_key_shape_the_real_ledger_has(store, tmp_path):
    """Measured against from-server/brief_memory.json: 25 rows, and `status`,
    `origin`, `driver`, `horizon_days`, `extractor_model` and `prompt_version`
    absent on ALL of them. The sibling importers' entry["name"] shape would
    fail on every row."""
    p = _write_ledger(
        tmp_path,
        [
            {
                "id": "c-0001",
                "claim": "a fact",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
                "source_count": 2,
                "severity": "normal",
            }
        ],
    )
    assert claim_store.import_legacy(store, p) == 1
    claim = claim_store.load_ledger(store)["claims"][0]
    assert claim["status"] == "standing"
    assert claim["origin"] == "extracted"
    assert "extractor_model" not in claim


def test_import_is_idempotent_because_it_guards_on_the_table(store, tmp_path):
    p = _write_ledger(
        tmp_path,
        [
            {
                "id": "c-0001",
                "claim": "a fact",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
            }
        ],
    )
    assert claim_store.import_legacy(store, p) == 1
    assert claim_store.import_legacy(store, p) == 0


def test_an_empty_claims_list_is_not_mistaken_for_already_imported(store, tmp_path):
    """The guard reads the TABLE, not the file: the ledger is a dict with a
    claims list, not a bare list, so `{"claims": []}` must import nothing and
    still leave the table importable later."""
    assert claim_store.import_legacy(store, _write_ledger(tmp_path, [])) == 0
    p = _write_ledger(
        tmp_path,
        [
            {
                "id": "c-0001",
                "claim": "a fact",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
            }
        ],
    )
    assert claim_store.import_legacy(store, p) == 1


def test_a_malformed_file_imports_nothing_and_does_not_raise(store, tmp_path):
    """It runs at boot. It must not be able to stop one."""
    p = tmp_path / "brief_memory.json"
    p.write_text("{not json", encoding="utf-8")
    assert claim_store.import_legacy(store, p) == 0


def test_a_missing_file_imports_nothing(store, tmp_path):
    assert claim_store.import_legacy(store, tmp_path / "nope.json") == 0


def test_a_non_standing_row_without_broke_on_gets_an_approximate_date(store, tmp_path):
    """CHECK (status = 'standing' OR resolved_on IS NOT NULL) (0006:209) would
    reject it, and a per-row skip is silent. _apply_status only began stamping
    broke_on when jx9.x shipped, so such rows can exist. An approximate date
    that is logged beats an invented one, and beats a dropped row."""
    p = _write_ledger(
        tmp_path,
        [
            {
                "id": "c-0001",
                "claim": "a fact",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
                "status": "broken",
                "broken_by": "a reversal",
            }
        ],
    )
    assert claim_store.import_legacy(store, p) == 1
    assert claim_store.load_ledger(store)["claims"][0]["broke_on"] == "2026-06-24"


def test_a_row_without_an_id_is_rejected_loudly(store, tmp_path):
    """merge_ledger re-appends an id-less prior row (it is excluded from by_id
    but caught by the trailing loop), so one can exist in the file. It has no
    ledger_id to upsert against and would vanish silently in the diff."""
    p = _write_ledger(
        tmp_path,
        [
            {
                "claim": "no id",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
            },
            {
                "id": "c-0002",
                "claim": "fine",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
            },
        ],
    )
    assert claim_store.import_legacy(store, p) == 1
    assert [c["id"] for c in claim_store.load_ledger(store)["claims"]] == ["c-0002"]


def test_import_defaults_a_missing_last_reaffirmed_to_first_seen(store, tmp_path):
    """load_ledger treats a NULL last_reaffirmed as a hard error, so the import
    must never create one."""
    p = _write_ledger(
        tmp_path,
        [
            {"id": "c-0001", "claim": "a fact", "first_seen": "2026-06-01"},
        ],
    )
    assert claim_store.import_legacy(store, p) == 1
    assert (
        claim_store.load_ledger(store)["claims"][0]["last_reaffirmed"] == "2026-06-01"
    )


def test_a_degraded_run_says_so_instead_of_vanishing(store):
    """render_established_block returns "" when it has nothing, which removes
    the section entirely -- so a database outage and a genuinely empty ledger
    are BYTE-IDENTICAL to the reader. The brief silently loses its memory,
    re-explains yesterday's facts, and nothing says why. The run row tells the
    operator; this line tells the reader."""
    assert "unavailable" in claim_store.degraded_block().lower()
    assert claim_store.degraded_block() != ""
