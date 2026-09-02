"""Constraint tests for the knowledge base schema (migration 0006).

Every CHECK, unique key and trigger in 0006 gets a test that tries to violate
it. A constraint nothing attempts to break is a comment: spec 12.2's argument
is that a field can look correct and carry no information, and the same is
true of a constraint nothing exercises.
"""

import contextlib
import re

import psycopg
import pytest

import brief_memory
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing.

    Module-local, matching tests/test_db.py and tests/test_config.py. Shared in
    conftest it would be reachable from every test module by parameter name
    alone, and it runs DROP SCHEMA public CASCADE.
    """
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    """A fully migrated database, including 0006."""
    db.run_migrations(conn)
    conn.commit()
    return conn


@contextlib.contextmanager
def rejects(conn, exc):
    """Assert the block raises `exc` AND leave the transaction usable.

    db.connect sets autocommit=False (db.py:110), so a constraint violation
    aborts the whole transaction and every later statement raises
    InFailedSqlTransaction -- a sibling of the error you expected, not a
    subclass, so the test fails on the wrong line with the wrong message.
    conn.transaction() opens a SAVEPOINT when already inside a transaction and
    rolls back to it, so the next assertion in the same test still works.

    Use this for EVERY expected violation, even where a bare pytest.raises
    would happen to work today -- the next author to add a second assertion to
    the test will not know to change it.
    """
    with pytest.raises(exc):
        with conn.transaction():
            yield


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _outlet(conn, name="Reuters"):
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _entity(conn, name="Apple", type_="company"):
    return conn.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id",
        (name, type_),
    ).fetchone()[0]


def test_outlet_kind_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, kind) VALUES ('X', 'blog')")


def test_perspective_null_is_allowed_and_a_bad_value_is_not(kb):
    """NULL means "no vantage claim made", NOT "neutral" -- calling a source
    neutral is a positive editorial claim, as contestable as picking a side."""
    kb.execute("INSERT INTO outlets (name, perspective) VALUES ('A', NULL)")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, perspective) VALUES ('B', 'NEUTRAL')")


def test_entity_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO entities (name, type) VALUES ('X', 'thing')")


def test_an_outlet_name_cannot_be_reused(kb):
    _outlet(kb, "Reuters")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _outlet(kb, "Reuters")


def test_an_outlet_name_collides_case_insensitively(kb):
    """outlets_name is UNIQUE (lower(name)), not (name): Reuters and reuters
    must be the same outlet, or per-outlet corroboration -- the entire reason
    outlets was split out of sources, spec 3.1 -- double-counts a single
    outlet as two."""
    _outlet(kb, "Reuters")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _outlet(kb, "reuters")


def test_an_entity_name_and_type_cannot_be_reused(kb):
    _entity(kb, "Apple", "company")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _entity(kb, "Apple", "company")


def test_an_entity_name_collides_case_insensitively_within_a_type(kb):
    """entities_name_type is UNIQUE (lower(name), type), not (name, type):
    Apple and apple must not become two entities, or per-entity aggregates
    split -- the "Links double and per-entity aggregates split" failure spec
    2.2 names."""
    _entity(kb, "Apple", "company")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _entity(kb, "apple", "company")


def test_the_same_name_under_a_different_type_is_a_different_entity(kb):
    """Spec 2.2: this key does NOT enforce one-entity-per-company -- putting
    `type` in the key means the same name is deliberately two rows when the
    type differs, e.g. a country and the institution that governs it."""
    _entity(kb, "Turkey", "country")
    _entity(kb, "Turkey", "institution")
    assert kb.execute("SELECT count(*) FROM entities").fetchone()[0] == 2


def test_the_same_text_from_two_outlets_is_two_items(kb):
    """Syndicated wire copy must not collapse. Spec 3.4.

    A global UNIQUE (content_hash) would attribute a Reuters story carried by
    five outlets to whichever was ingested first, capping corroboration at one
    -- the opposite of why outlets was split out of sources.
    """
    for outlet_id in (_outlet(kb, "Reuters"), _outlet(kb, "Guardian")):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u', 't', 'HASH')",
            (outlet_id,),
        )
    assert kb.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_the_same_text_twice_from_one_outlet_is_rejected(kb):
    outlet_id = _outlet(kb)
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'HASH')",
        (outlet_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u2', 't2', 'HASH')",
            (outlet_id,),
        )


def test_two_instrument_mappings_with_no_market_collide(kb):
    """NULLS NOT DISTINCT, spec 3.4. Under default Postgres semantics NULLs
    compare distinct, so both rows would insert and one symbol would map to the
    same entity twice."""
    entity_id = _entity(kb, "Apple")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (entity_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
            "VALUES (%s, 'AAPL', 'equity')",
            (entity_id,),
        )


def test_asset_class_admits_index_not_only_equity_and_crypto(kb):
    """The commodity-signals-are-index-class bug: "no instrument for BRENT" was
    a wrong asset CLASS, not a missing symbol."""
    entity_id = _entity(kb, "Brent", "instrument")
    for cls in ("equity", "index", "crypto", "commodity", "fx"):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, market, asset_class) "
            "VALUES (%s, %s, 'X', %s)",
            (entity_id, f"S{cls}", cls),
        )
    assert kb.execute("SELECT count(*) FROM entity_instruments").fetchone()[0] == 5


def _event(conn, type_="action", commitment="in_force"):
    return conn.execute(
        "INSERT INTO events (summary, type, commitment_state) "
        "VALUES ('s', %s, %s) RETURNING id",
        (type_, commitment),
    ).fetchone()[0]


def _item(conn, outlet_id, content_hash="H"):
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', %s) RETURNING id",
        (outlet_id, content_hash),
    ).fetchone()[0]


def test_type_and_commitment_state_vary_independently(kb):
    """Spec 3.2: three orthogonal fields, because geopolitical reporting mixes
    things that happened, things people said, and things people claim
    happened, and one field cannot carry that."""
    for type_ in ("action", "statement", "disclosure"):
        for commitment in ("in_force", "committed", "intended", "proposed"):
            _event(kb, type_, commitment)
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 12


def test_event_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "rumour")


def test_commitment_state_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "action", "mooted")


def test_an_event_can_supersede_another(kb):
    """Rule 1 marks the contradicted event superseded. Presence of the FK IS
    the state -- no status enum, so a single writer cannot make it degenerate."""
    old, new = _event(kb), _event(kb)
    kb.execute("UPDATE events SET superseded_by = %s WHERE id = %s", (new, old))
    assert (
        kb.execute("SELECT superseded_by FROM events WHERE id = %s", (old,)).fetchone()[
            0
        ]
        == new
    )


def test_standing_rejects_an_unknown_value(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'rumoured')",
            (item_id, event_id),
        )


def test_source_relationship_is_nullable_with_no_default(kb):
    """Spec 2.1: the one extracted enum with no anchor gets NO default -- a
    NOT NULL DEFAULT is the fastest route to the degenerate outcome 12.2 rates
    worse than a missing field. Absent means "not labelled", never
    "independent"."""
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    assert (
        kb.execute("SELECT source_relationship FROM assertions").fetchone()[0] is None
    )


def test_one_item_asserts_one_event_once(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'official')",
            (item_id, event_id),
        )


def _observation(conn, entity_id, metric="price", value=100, window=None):
    return conn.execute(
        "INSERT INTO observations (entity_id, symbol, metric, value, return_window, "
        "observed_at, provider) VALUES (%s, 'S', %s, %s, %s, now(), 'yahoo') RETURNING id",
        (entity_id, metric, value, window),
    ).fetchone()[0]


def test_a_return_without_a_window_is_rejected(kb):
    """Spec 2.2: a return without a period is not a number."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "return", 0.13, None)


def test_a_price_with_a_window_is_rejected(kb):
    """The biconditional runs both ways: a level has no window."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "price", 100, "1d")


def test_a_return_and_a_price_are_two_separate_rows(kb):
    """The replay's "SK Hynix +13%" is a return row; the level it moved from is
    a separate price row. Conflating them is how a one-day bounce became
    confirmation of a multi-quarter thesis."""
    entity_id = _entity(kb, "SK Hynix")
    _observation(kb, entity_id, "return", 0.13, "1d")
    _observation(kb, entity_id, "price", 100)
    assert kb.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_metric_rejects_an_unknown_value(kb):
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "sentiment")


def test_observations_carry_provider_not_extractor_model(kb):
    """Spec 3.5: observations are FETCHED, not extracted. A column named
    extractor_model holding 'yahoo' would be a lie in the one field that exists
    to make silent model drift detectable."""
    cols = {
        r[0]
        for r in kb.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'observations'"
        ).fetchall()
    }
    assert "provider" in cols
    assert "extractor_model" not in cols


def _claim(conn, text="a claim", status="standing", resolved_on=None, **kw):
    cols = ["claim", "status", "first_seen", "resolved_on"]
    vals = [text, status, "2026-09-01", resolved_on]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO claims ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        vals,
    ).fetchone()[0]


def test_status_admits_all_six_states(kb):
    """Spec 2.1: one lifecycle, one column. resolved_outcome does not exist --
    two enums with 'broken' in both permitted status='standing' alongside
    resolved_outcome='broken', and left rule 4's 'stale' homeless."""
    for status in (
        "standing",
        "challenged",
        "broken",
        "confirmed",
        "expired",
        "withdrawn",
    ):
        resolved = None if status == "standing" else "2026-09-02"
        extra = {"broken_by_note": "n"} if status == "broken" else {}
        _claim(kb, f"c {status}", status, resolved, **extra)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 6


def test_status_rejects_stale_which_is_spelled_expired_here(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "stale", "2026-09-02")


def test_a_non_standing_claim_must_record_when_it_left_standing(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "expired", None)


def test_horizon_elapsed_requires_a_resolution(kb):
    """Elapsed days mean nothing without the resolution they are measured to."""
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "standing", None, horizon_elapsed=2)


def test_a_broken_claim_must_say_what_broke_it(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "broken", "2026-09-02")


def test_a_broken_claim_accepts_either_a_note_or_an_event(kb):
    """The incumbent writes free text ('unmarked rewrite: ...'); rule 1 wants
    the contradicting event. A single BIGINT FK could not hold the existing
    values, which is why there are two columns."""
    event_id = _event(kb)
    _claim(kb, "c1", "broken", "2026-09-02", broken_by_note="unmarked rewrite: x")
    _claim(kb, "c2", "broken", "2026-09-02", broken_by_event_id=event_id)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 2


def test_horizon_days_rejects_zero_negatives_and_over_ten_years(kb):
    """Matches the 1..3650 range _coerce_horizon_days already enforces
    (_MAX_HORIZON_DAYS). Without it, resolution_date lands on or before
    first_seen and rule 4 fires the moment the claim is created.

    Three assertions in one test: this is exactly why `rejects` uses a
    savepoint. A bare pytest.raises would leave the transaction aborted and the
    second iteration would raise InFailedSqlTransaction instead.
    """
    for bad in (0, -1, 3651):
        with rejects(kb, psycopg.errors.CheckViolation):
            _claim(kb, f"c{bad}", horizon_days=bad)


def test_horizon_days_null_is_allowed_and_means_unknown(kb):
    """Spec 2.2: absence is not a short horizon. Never defaulted -- a default
    would manufacture calibration data."""
    claim_id = _claim(kb, "c", horizon_days=None)
    assert (
        kb.execute(
            "SELECT horizon_days FROM claims WHERE id = %s", (claim_id,)
        ).fetchone()[0]
        is None
    )


def test_claims_without_a_ledger_ancestor_coexist(kb):
    """ledger_id is UNIQUE but nullable, and NULLs are DISTINCT by default --
    deliberately, so KB-native claims need no synthetic id."""
    _claim(kb, "c1")
    _claim(kb, "c2")
    assert (
        kb.execute("SELECT count(*) FROM claims WHERE ledger_id IS NULL").fetchone()[0]
        == 2
    )


def test_a_ledger_id_cannot_be_reused(kb):
    """merge_ledger treats an echoed id as authoritative, so two rows claiming
    c-0001 would make the model's citation ambiguous."""
    _claim(kb, "c1", ledger_id="c-0001")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _claim(kb, "c2", ledger_id="c-0001")


def test_evidence_pointing_at_neither_is_rejected(kb):
    claim_id = _claim(kb, "c")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO claim_evidence (claim_id) VALUES (%s)", (claim_id,))


def test_evidence_pointing_at_both_is_rejected(kb):
    """The both-set case is the one that silently corrupts rule 3's count."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    observation_id = _observation(kb, _entity(kb, "SK Hynix"))
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO claim_evidence (claim_id, event_id, observation_id) "
            "VALUES (%s, %s, %s)",
            (claim_id, event_id, observation_id),
        )


def test_the_same_evidence_cannot_be_counted_twice(kb):
    """NULLS NOT DISTINCT. Under default semantics both rows insert, one piece
    of evidence counts as two, and the evidence floor is cleared by a
    duplicate -- the chip-whipsaw failure rule 3 exists to prevent."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    kb.execute(
        "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
        (claim_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
            (claim_id, event_id),
        )


def test_deleting_an_event_cannot_silently_lower_the_evidence_floor(kb):
    """RESTRICT, not CASCADE: a floor an unrelated delete can lower is not a
    floor."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    kb.execute(
        "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
        (claim_id, event_id),
    )
    with rejects(kb, psycopg.errors.RestrictViolation):
        kb.execute("DELETE FROM events WHERE id = %s", (event_id,))


def test_a_standing_claim_may_still_be_refined(kb):
    """Negative control: rewording stays correct for a claim that is, and
    remains, standing. Expected to pass BEFORE the trigger exists too."""
    claim_id = _claim(kb, "original")
    kb.execute("UPDATE claims SET claim = 'refined' WHERE id = %s", (claim_id,))
    assert (
        kb.execute("SELECT claim FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        == "refined"
    )


def test_an_already_broken_claim_cannot_be_reworded(kb):
    claim_id = _claim(kb, "original", "broken", "2026-09-02", broken_by_note="n")
    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute("UPDATE claims SET claim = 'rewritten' WHERE id = %s", (claim_id,))


def test_breaking_and_rewriting_in_one_statement_is_rejected(kb):
    """THE Patriot mechanism. One reply marked the claim broken AND rewrote it
    into a description of its own reversal, so the ledger read back as though
    the reversal had itself been reversed.

    A predicate reading OLD.status alone lets this through, because OLD.status
    is still 'standing' at that moment. This test is why the trigger reads both
    tuples, and it is the one a test written to the constraint rather than to
    the failure would miss.
    """
    claim_id = _claim(kb, "Trump agreed to license Patriot production")
    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute(
            "UPDATE claims SET status = 'broken', resolved_on = '2026-09-02', "
            "broken_by_note = 'reversed', claim = 'Trump reversed course' "
            "WHERE id = %s",
            (claim_id,),
        )


def test_the_status_may_change_without_touching_the_text(kb):
    """Breaking a claim is normal; only rewriting it is forbidden. The reversal
    belongs in broken_by, not in the claim text."""
    claim_id = _claim(kb, "original")
    kb.execute(
        "UPDATE claims SET status = 'broken', resolved_on = '2026-09-02', "
        "broken_by_note = 'n' WHERE id = %s",
        (claim_id,),
    )
    assert (
        kb.execute("SELECT claim FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        == "original"
    )


def test_a_broken_claim_cannot_be_moved_back_to_standing(kb):
    """The terminality spec 2.3 and 6 assert: broken/confirmed/expired/
    withdrawn are terminal 'in this schema'. Before this clause, neither CHECK
    nor the text-freeze trigger stopped this UPDATE -- the first CHECK
    short-circuits true on 'status = standing', the second short-circuits true
    because the new status is not 'broken', and the trigger only fires when
    claim text changes, which this statement does not touch."""
    claim_id = _claim(kb, "original", "broken", "2026-09-02", broken_by_note="n")
    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute("UPDATE claims SET status = 'standing' WHERE id = %s", (claim_id,))


def test_a_challenged_claim_can_still_be_moved_back_to_standing(kb):
    """Negative control for the test above: 'challenged' is deliberately NOT
    terminal, spec 2.3's 'standing -> challenged -> standing is permitted,
    matching _apply_status'. If this fails, the terminal-status clause is too
    broad."""
    claim_id = _claim(kb, "original", "challenged", "2026-09-02")
    kb.execute("UPDATE claims SET status = 'standing' WHERE id = %s", (claim_id,))
    assert (
        kb.execute("SELECT status FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        == "standing"
    )


def test_a_terminal_claim_still_accepts_a_non_status_update(kb):
    """The terminal-status clause guards the status COLUMN, not the row.
    Provenance stamping and similar non-status writes to an already-broken
    claim must keep working."""
    claim_id = _claim(kb, "original", "broken", "2026-09-02", broken_by_note="n")
    kb.execute(
        "UPDATE claims SET extractor_model = 'sonnet-5' WHERE id = %s", (claim_id,)
    )
    assert (
        kb.execute(
            "SELECT extractor_model FROM claims WHERE id = %s", (claim_id,)
        ).fetchone()[0]
        == "sonnet-5"
    )


KB_TABLES = {
    "outlets",
    "items",
    "entities",
    "entity_instruments",
    "events",
    "event_entities",
    "assertions",
    "observations",
    "claims",
    "claim_evidence",
    "theses",
    "thesis_claims",
    "stories",
    "story_members",
    "open_questions",
    "links",
}


def _story(conn, name="Hormuz", scope="structural"):
    return conn.execute(
        "INSERT INTO stories (name, scope) VALUES (%s, %s) RETURNING id", (name, scope)
    ).fetchone()[0]


def _thesis(conn, text="t"):
    return conn.execute(
        "INSERT INTO theses (text) VALUES (%s) RETURNING id", (text,)
    ).fetchone()[0]


def test_0006_creates_all_sixteen_kb_tables(kb):
    assert KB_TABLES <= _tables(kb)


def test_confidence_defaults_to_speculative(kb):
    """Spec 2.3: speculative means NO supporting claims at all. An earlier
    draft said "none resolved", which overlapped tentative completely and left
    the ladder undetermined at the value every thesis starts on."""
    _thesis(kb)
    assert kb.execute("SELECT confidence FROM theses").fetchone()[0] == "speculative"


def test_a_claim_supports_or_undermines_a_thesis_but_not_both(kb):
    thesis_id, claim_id = _thesis(kb), _claim(kb, "c")
    kb.execute(
        "INSERT INTO thesis_claims (thesis_id, claim_id, role) "
        "VALUES (%s, %s, 'supporting')",
        (thesis_id, claim_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO thesis_claims (thesis_id, claim_id, role) "
            "VALUES (%s, %s, 'undermining')",
            (thesis_id, claim_id),
        )


def test_a_new_story_is_active_with_no_material_change(kb):
    """Spec 2.3: the state must be defined at the default. NULL
    last_material_change means "created, no members yet" and reads active --
    the same hole that made the old confidence ladder undetermined."""
    _story(kb)
    assert kb.execute("SELECT state, last_material_change FROM stories").fetchone() == (
        "active",
        None,
    )


def test_story_scope_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _story(kb, "X", "ongoing")


def test_a_story_cannot_hold_the_same_event_twice(kb):
    """3.5 forbids rebuilding the member list, so a duplicate row renders as a
    duplicate line in the brief with no read-time dedup to catch it."""
    story_id, event_id = _story(kb, "S", "episodic"), _event(kb)
    kb.execute(
        "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
        (story_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
            (story_id, event_id),
        )


def test_deleting_an_event_cannot_retcon_a_member_list(kb):
    """A cascade here is a silent retcon of the stored list -- the Day 3
    failure mechanism."""
    story_id, event_id = _story(kb, "S", "episodic"), _event(kb)
    kb.execute(
        "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
        (story_id, event_id),
    )
    with rejects(kb, psycopg.errors.RestrictViolation):
        kb.execute("DELETE FROM events WHERE id = %s", (event_id,))


def test_a_link_must_carry_a_decay_check_date(kb):
    """NOT NULL, derived from expected_persistence at write time. A nullable
    decay date leaves a link 'unchecked' forever, never entering rule 5 -- and
    2.3's negative case makes that permanent rather than eventually-noticed."""
    observation_id = _observation(kb, _entity(kb, "Brent", "instrument"))
    with rejects(kb, psycopg.errors.NotNullViolation):
        kb.execute(
            "INSERT INTO links (event_id, observation_id, mechanism, effect_kind, "
            "expected_persistence) VALUES (%s, %s, 'm', 'flow', 'session')",
            (_event(kb), observation_id),
        )


def test_effect_kind_admits_all_four_decay_behaviours(kb):
    """Confusing a re_rating driver with flow is what produced three
    contradictory chip verdicts in 72 hours."""
    observation_id = _observation(kb, _entity(kb, "Brent", "instrument"))
    for kind in ("re_rating", "risk_premium", "flow", "fundamental_revision"):
        kb.execute(
            "INSERT INTO links (event_id, observation_id, mechanism, effect_kind, "
            "expected_persistence, decay_check_date) "
            "VALUES (%s, %s, 'm', %s, 'days', '2026-09-10')",
            (_event(kb), observation_id, kind),
        )
    assert kb.execute("SELECT count(*) FROM links").fetchone()[0] == 4


def test_an_open_question_defaults_to_open(kb):
    story_id = _story(kb, "S", "episodic")
    kb.execute(
        "INSERT INTO open_questions (story_id, text) VALUES (%s, 'q')", (story_id,)
    )
    assert kb.execute("SELECT status FROM open_questions").fetchone()[0] == "open"


def _functions(conn) -> set[str]:
    rows = conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _copy_migrations(tmp_path):
    for p in db.MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_0006_rolls_back_and_reapplies_against_the_real_directory(conn):
    """The only test in this repo that executes a real down migration.

    Three things can go wrong and only this catches them: a wrong FK drop
    order, a function left behind by DROP TABLE, and a CREATE FUNCTION that
    should have been CREATE OR REPLACE. The last one shows only on the second
    up.

    steps=3 rolls back 0008 (capture telemetry, no tables of its own that
    reference the KB) and 0007 (retired_on, no tables of its own) as well as
    0006, since "down" with no steps reverts only the most recent migration
    and 0008 now sits on top of 0007 which sits on top of 0006.
    """
    db.run_migrations(conn)
    conn.commit()
    assert KB_TABLES <= _tables(conn)
    assert "claims_freeze_claim_text" in _functions(conn)

    reverted = db.run_migrations(conn, direction="down", steps=3)
    conn.commit()

    assert reverted == [
        "0008_capture_telemetry",
        "0007_claim_retirement",
        "0006_knowledge_base",
    ]
    assert not (KB_TABLES & _tables(conn)), "a KB table survived the rollback"
    assert "claims_freeze_claim_text" not in _functions(conn), (
        "DROP TABLE does not drop a function; the down migration must drop it"
    )
    assert {"users", "settings", "job_runs", "sources"} <= _tables(conn), (
        "rolling back one step must not take the prior migrations with it"
    )

    reapplied = db.run_migrations(conn)
    conn.commit()
    assert reapplied == [
        "0006_knowledge_base",
        "0007_claim_retirement",
        "0008_capture_telemetry",
    ]
    assert KB_TABLES <= _tables(conn)


def test_the_rollback_assertion_can_actually_fail(conn, tmp_path, monkeypatch):
    """Negative control for the test above, automated rather than manual.

    A rollback assertion that has never failed proves nothing. This copies the
    real migrations to a temp directory, strips DROP FUNCTION from 0006's down
    file, and asserts the function then SURVIVES the rollback -- which is what
    makes the assertion above meaningful.

    Done as a test rather than as a comment-out-and-restore ritual, because a
    manual negative control is the step that gets skipped under time pressure,
    and it leaves a window where an interrupt commits the migration without its
    function drop.

    steps=3 reaches 0006's (stripped) down migration: with no steps, "down"
    reverts only the most recent migration, and 0008 now sits on top of 0007
    which sits on top of 0006.
    """
    tmp = _copy_migrations(tmp_path)
    down = tmp / "0006_knowledge_base_down.sql"
    down.write_text(
        "\n".join(
            line
            for line in down.read_text(encoding="utf-8").splitlines()
            if "DROP FUNCTION" not in line
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp)

    db.run_migrations(conn)
    conn.commit()
    db.run_migrations(conn, direction="down", steps=3)
    conn.commit()

    assert "claims_freeze_claim_text" in _functions(conn), (
        "stripping DROP FUNCTION should leave the function behind; if it does "
        "not, the assertion in the test above cannot fail and proves nothing"
    )


LEDGER_KEY_TO_COLUMN = {
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

PROVISIONAL_TABLES = {
    "theses",
    "thesis_claims",
    "stories",
    "story_members",
    "open_questions",
    "observations",
    "entity_instruments",
    "links",
}


def status_check_values(kb):
    """The values claims.status actually permits, read off the live constraint.

    A hardcoded copy of the enum here would be a THIRD list beside the DDL and
    brief_memory._VALID_STATUS, and three hardcoded lists drift together in
    silence -- the failure mode is that everything agrees and everything is
    wrong. Postgres renders `CHECK (status IN (...))` as `status = ANY
    (ARRAY['standing'::text, ...])`, which is what the filter below keys on;
    the table's other status CHECKs (resolved_on, broken_by) use `status =` and
    `status <>` and are excluded by it.
    """
    defs = [
        r[0]
        for r in kb.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'claims'::regclass AND contype = 'c'"
        ).fetchall()
    ]
    enum = [d for d in defs if "status = ANY" in d]
    assert len(enum) == 1, f"expected exactly one status enum CHECK, got {enum}"
    values = set(re.findall(r"'([a-z_]+)'::text", enum[0]))
    # Prove the probe can return something before any test believes an answer
    # from it: an empty or one-element parse would make every comparison below
    # pass for the wrong reason.
    assert len(values) > 1, f"parsed no enum values out of {enum[0]!r}"
    return values


def test_every_ledger_key_has_a_claims_column(kb):
    """Spec 5.1, the WRITE direction. LEDGER_KEY_TO_COLUMN is a hand-written
    constant, not derived from brief_memory, so this cannot catch a ledger key
    ADDED without a column -- there is nothing here to diff a new key against.
    What it does catch is a claims column dropped or renamed out from under
    the map, which would otherwise surface halfway through bqa.4 as a column
    that no longer exists.

    IF THIS FAILS: a mapped claims column was dropped or renamed. Either
    restore it or update the map to the new name. Do not delete the entry.
    """
    columns = {
        r[0]
        for r in kb.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'claims'"
        ).fetchall()
    }
    missing = {k: c for k, c in LEDGER_KEY_TO_COLUMN.items() if c not in columns}
    assert not missing, f"ledger keys with no claims column: {missing}"


def test_every_status_the_schema_permits_survives_the_coercer(kb):
    """Spec section 6 item 1, the READ direction -- and the one that matters.

    _coerce_status returns None outside _VALID_STATUS and every caller falls
    back to 'standing', so a status the coercer does not know is not merely
    ignored: the TTL filter deletes it after 7 days and select_working_set
    renders it as live fact. A write-direction map alone is
    the-probe-measured-the-wrong-layer, and it is exactly what let this defect
    through two spec reviews.

    This carried a strict xfail until news-brief-bqa.9 widened the frozenset.
    """
    degrades = {
        s for s in status_check_values(kb) if brief_memory._coerce_status(s) != s
    }
    assert not degrades, (
        f"these statuses coerce away and will be TTL-deleted: {sorted(degrades)}. "
        "Widen brief_memory._VALID_STATUS and split the TTL and render predicates "
        "from '!= standing' to an explicit terminal set (news-brief-bqa.9)."
    )


def test_the_status_check_and_valid_status_are_the_same_set(kb):
    """The cross-layer invariant itself, in the only place both sides are
    visible at once: a Postgres CHECK and a Python frozenset, one I/O boundary
    apart, that no type checker or constraint can reconcile.

    The coercion test above catches a value the DDL gains and brief_memory does
    not. This catches the other direction too -- a value brief_memory gains that
    the DDL will reject on write -- and it is the reason tests/
    test_brief_memory.py may keep its single hardcoded copy of the six.

    IF THIS FAILS: the enum moved on one side only. Change the other side, or
    the next migration writes rows the reader silently reclassifies.
    """
    assert status_check_values(kb) == set(brief_memory._VALID_STATUS)


def test_no_source_file_writes_a_provisional_table():
    """Spec 1.2's reshaping licence lasts only until something WRITES a
    provisional table, and nothing observes first write against a live
    database -- the `conn` fixture drops and recreates the schema for every
    test, so every provisional table is empty BY CONSTRUCTION on every run.
    An emptiness assertion against that fixture is true of the fixture, not
    of the world, and can never fail.

    This scans the repository's own Python sources instead -- the layer that
    actually moves when the licence expires -- for an INSERT or UPDATE naming
    a provisional table, at the repo root and under enrichment/ (application
    code lives there; tests/ is excluded because the fixtures above legitimately
    write these tables).

    IF THIS FAILS: application code has started writing one of
    PROVISIONAL_TABLES, which is expected and good -- go re-read spec 1.2,
    decide whether the shape just written is now frozen, and narrow
    PROVISIONAL_TABLES to drop the table that gained a writer. Do not delete
    this test.
    """
    root = db.MIGRATIONS_DIR.parent
    sources = list(root.glob("*.py"))
    enrichment_dir = root / "enrichment"
    if enrichment_dir.is_dir():
        sources += [
            p for p in enrichment_dir.rglob("*.py") if "__pycache__" not in p.parts
        ]
    offenders = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for table in PROVISIONAL_TABLES:
            if re.search(rf"\b(insert\s+into|update)\s+{table}\b", text, re.IGNORECASE):
                offenders.append(f"{path.relative_to(root)}: {table}")
    assert not offenders, f"provisional table written by application code: {offenders}"
