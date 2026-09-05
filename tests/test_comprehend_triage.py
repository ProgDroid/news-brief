"""Triage stage: the rules half, the model half, and the sampled control arm."""

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def test_a_disabled_run_writes_no_triage_rows(kb, monkeypatch):
    """Ships off. The disabled path must be a real no-op, not a path that
    happens to find nothing -- so this test seeds an item that WOULD be
    triaged if the flag were on."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", False)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0
    assert tally.items_seen == 0
    assert tally.enabled is False


def test_an_enabled_run_sees_the_item(kb, monkeypatch):
    """Presence sibling. Without it, the disabled assertion above is satisfied
    for free by a run() that does nothing under any setting."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.enabled is True
    assert tally.items_seen == 1


def _outlet(kb, name="Reuters"):
    """Get-or-create. `outlets` is UNIQUE (lower(name)) (0006:25), so a bare
    INSERT would fail on the second call within one test."""
    row = kb.execute(
        "SELECT id FROM outlets WHERE lower(name) = lower(%s)", (name,)
    ).fetchone()
    if row:
        return row[0]
    return kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _add_item(kb, title, body=None, outlet_id=None, h="H1"):
    """`items` is UNIQUE (outlet_id, content_hash), so callers adding more than
    one item to the same outlet must pass distinct `h`."""
    outlet_id = outlet_id or _outlet(kb)
    return kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, published_at) "
        "VALUES (%s, 'u', %s, %s, %s, now()) RETURNING id",
        (outlet_id, title, body, h),
    ).fetchone()[0]


def test_a_tracked_entity_makes_an_item_material_with_no_model_call(kb):
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    hit = comprehend.triage_by_rules(item, index)

    assert hit is not None
    assert hit.reason == "tracked_entity"
    comprehend.record_triage(
        kb, item_id, "material", hit.reason, None, comprehend.TRIAGE_PROMPT_VERSION
    )
    kb.commit()
    row = kb.execute(
        "SELECT verdict, reason, triage_model FROM item_triage WHERE item_id = %s",
        (item_id,),
    ).fetchone()
    assert row == ("material", "tracked_entity", None), (
        "triage_model must be NULL when no model ran -- a NOT NULL value here "
        "would make the rules half indistinguishable from the model half"
    )


def test_an_untracked_item_is_not_matched_by_the_rules(kb):
    """Absence assertion; the test above is its presence sibling."""
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    item_id = _add_item(kb, "Chip export controls tightened")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    assert comprehend.triage_by_rules(item, index) is None
    assert item_id  # the item exists; it simply did not match


def test_the_rules_half_reads_the_body_as_well_as_the_title(kb):
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    _add_item(kb, "A ceasefire is signalled", body="Officials in Ukraine confirmed.")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    assert comprehend.triage_by_rules(item, index) is not None


def test_a_live_claim_topic_is_tracked_but_a_terminal_one_is_not(kb):
    # first_seen is DATE NOT NULL with no default (0006:193): omitting it
    # raises NotNullViolation. last_reaffirmed is nullable (0006:194) but is
    # supplied anyway, because claim_store._row_to_claim treats a NULL there as
    # a HARD ERROR -- the column's nullability and the loader's contract
    # disagree, and a fixture that exercises the disagreement will confuse
    # whoever debugs it next.
    kb.execute(
        "INSERT INTO claims (claim, topic, status, first_seen, last_reaffirmed) "
        "VALUES ('c', 'Sahel', 'standing', CURRENT_DATE, CURRENT_DATE)"
    )
    kb.execute(
        "INSERT INTO claims (claim, topic, status, resolved_on, first_seen, "
        "  last_reaffirmed) "
        "VALUES ('d', 'Balkans', 'withdrawn', CURRENT_DATE, CURRENT_DATE, CURRENT_DATE)"
    )
    kb.commit()
    index = comprehend.SurfaceIndex.build(kb)

    assert [f.reason for f in index.match("Sahel unrest deepens")] == ["tracked_claim"]
    assert index.match("Balkans unrest deepens") == [], (
        "a terminal claim's topic must not be tracked, or every settled "
        "question stays permanently material"
    )


def test_a_triaged_item_is_not_returned_again_at_the_same_version(kb):
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()
    assert len(comprehend.pending_triage(kb, 1, 10)) == 1

    comprehend.record_triage(kb, item_id, "immaterial", "none", None, 1)
    kb.commit()

    assert comprehend.pending_triage(kb, 1, 10) == []
    assert len(comprehend.pending_triage(kb, 2, 10)) == 1, (
        "but a NEW triage prompt version must see it again"
    )


def test_a_failed_item_is_retried_until_the_ceiling(kb):
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()
    comprehend.record_triage(kb, item_id, "failed", "error", None, 1)
    kb.commit()
    assert len(comprehend.pending_triage(kb, 1, 10)) == 1, "attempts=1 is retryable"

    kb.execute("UPDATE item_triage SET attempts = 3 WHERE item_id = %s", (item_id,))
    kb.commit()
    assert comprehend.pending_triage(kb, 1, 10) == [], (
        "at the ceiling it stops being selected, or it re-pays model cost forever"
    )


def test_a_second_triage_at_the_same_version_bumps_attempts_and_overwrites(kb):
    """The increment the ceiling depends on, exercised through the real path.

    The ceiling test reaches attempts=3 with a raw UPDATE, so nothing calls
    record_triage twice and the ON CONFLICT branch never runs -- delete
    `attempts = item_triage.attempts + 1` and the suite stays green while a
    failed item re-pays model cost on every pass, forever.
    """
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()
    comprehend.record_triage(kb, item_id, "failed", "error", None, 1)
    comprehend.record_triage(kb, item_id, "material", "tracked_entity", None, 1)
    kb.commit()

    rows = kb.execute(
        "SELECT verdict, reason, attempts FROM item_triage WHERE item_id = %s",
        (item_id,),
    ).fetchall()
    assert len(rows) == 1, (
        "the unique key on (item_id, triage_prompt_version) must collapse the "
        "retry onto one row rather than inserting a second"
    )
    assert rows[0][0] == "material", "EXCLUDED must overwrite the stale verdict"
    assert rows[0][1] == "tracked_entity"
    assert rows[0][2] == 2, (
        "the second call bumps attempts; without the bump the ceiling at 3 is "
        "unreachable and a failing item retries forever"
    )


def test_a_tracked_story_name_makes_an_item_material(kb):
    """The third arm of the tracked half. Entity and claim forms are both
    covered; without this, a story form could stop producing a hit and only
    the build() test would notice -- and that one checks the index, not the
    verdict the index produces."""
    index = comprehend.SurfaceIndex(
        [comprehend.SurfaceForm("Black Sea shipping", "tracked_story", None)]
    )
    hit = comprehend.triage_by_rules(
        {"title": "Black Sea shipping resumes after talks", "body": None}, index
    )
    assert hit is not None, "a tracked story name in the title is a material hit"
    assert hit.reason == "tracked_story"

    miss = comprehend.triage_by_rules({"title": "Weather report", "body": None}, index)
    assert miss is None, (
        "presence sibling for the assertion above: an unrelated title must NOT "
        "match, or `hit is not None` passes for a matcher that matches everything"
    )


def _tool_use(items):
    return {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "emit_triage",
                "input": {"items": items},
            }
        ],
    }


def test_the_triage_request_forces_the_tool_and_disables_thinking():
    req = comprehend.build_triage_request(
        [{"id": 1, "title": "t", "body": "b", "outlet": "Reuters"}]
    )
    assert req["tool_choice"] == {"type": "tool", "name": "emit_triage"}
    assert req["thinking"] == {"type": "disabled"}, (
        "a forced-tool extraction on a tight budget must disable thinking: "
        "Sonnet 5 runs ADAPTIVE thinking when it is omitted, which eats "
        "max_tokens and truncates"
    )


def test_a_truncated_response_raises_rather_than_being_parsed():
    """stop_reason is checked BEFORE the parser. Four recorded recurrences of
    a truncation being misdiagnosed as a broken parser."""
    resp = _tool_use([{"id": 1, "material": True}])
    resp["stop_reason"] = "max_tokens"
    with pytest.raises(ValueError, match="truncated"):
        comprehend.parse_triage_response(resp, {1})


def test_a_well_formed_response_is_parsed():
    """Presence sibling for the truncation test: an always-raising parser
    would satisfy that one for free."""
    resp = _tool_use([{"id": 1, "material": True}, {"id": 2, "material": False}])
    assert comprehend.parse_triage_response(resp, {1, 2}) == {1: True, 2: False}


def test_an_id_that_was_never_offered_is_dropped():
    """The model can return an id we did not send. Trusting it would write a
    verdict against an unrelated item."""
    resp = _tool_use([{"id": 1, "material": True}, {"id": 999, "material": True}])
    assert comprehend.parse_triage_response(resp, {1}) == {1: True}


def test_a_missing_tool_block_raises():
    with pytest.raises(ValueError, match="emit_triage"):
        comprehend.parse_triage_response(
            {"stop_reason": "end_turn", "content": []}, {1}
        )


def test_a_tool_block_without_an_items_list_raises():
    """The tool can fire with `input` present and no `items` in it. Without
    this test the isinstance guard is a comment -- nothing enters it, and
    deleting it turns a clear ValueError at the boundary into a TypeError deep
    in the row loop, where the message names neither the tool nor the cause."""
    resp = {
        "content": [
            {"type": "tool_use", "name": "emit_triage", "input": {"verdicts": []}}
        ]
    }
    with pytest.raises(ValueError, match="items"):
        comprehend.parse_triage_response(resp, {1})


def test_a_malformed_row_is_dropped_without_losing_its_neighbours():
    """Rows are filtered one at a time, not validated as a batch: a single bad
    row must not discard the good ones alongside it. Asserts BOTH halves in one
    equality -- the malformed rows are absent AND the well-formed row survives.
    An `== {}` assertion would pass for a parser that dropped everything, which
    is the failure this shape is meant to exclude."""
    resp = {
        "content": [
            {
                "type": "tool_use",
                "name": "emit_triage",
                "input": {
                    "items": [
                        {"id": "7", "material": True},  # id is a string
                        {"id": 8, "material": "yes"},  # material is a string
                        {"id": 9, "material": True},  # the only well-formed row
                        "not even a dict",
                    ]
                },
            }
        ]
    }
    assert comprehend.parse_triage_response(resp, {7, 8, 9}) == {9: True}
