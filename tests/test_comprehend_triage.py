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
    for free by a run() that does nothing under any setting.

    Now that run() is wired end to end (Task 10), an unmatched item falls
    through to the model triage half, and the sampled control arm can promote
    an immaterial item straight into the integration step. Stub call_triage so
    the item never reaches a real socket, and zero the sample budget so this
    item -- deliberately marked immaterial -- can't be promoted into a call to
    the (unstubbed) call_integration.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)

    def fake_triage(req):
        ids = [
            int(line.split("id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if line.startswith("- id=")
        ]
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": i, "material": False} for i in ids]},
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)

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


def test_a_boolean_id_cannot_overwrite_a_genuine_verdict():
    """The genuine row comes FIRST here, and that ordering is the whole test.

    `hash(True) == hash(1)`, so both rows address the same dict slot. With the
    boolean row second, an unguarded parser writes `out[1] = True` and then
    lets `out[True] = False` land on top of it -- the garbage row does not
    appear as an obvious extra entry, it silently replaces a correct verdict
    for a real item. With the rows the other way round the later genuine write
    masks the bug and the assertion passes either way, which is what an earlier
    version of this test did.
    """
    resp = {
        "content": [
            {
                "type": "tool_use",
                "name": "emit_triage",
                "input": {
                    "items": [
                        {"id": 1, "material": True},
                        {"id": True, "material": False},
                    ]
                },
            }
        ]
    }
    assert comprehend.parse_triage_response(resp, {1}) == {1: True}, (
        "the boolean row must be dropped; unguarded it overwrites item 1's "
        "verdict with False and nothing errors, because the id is real"
    )


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


def test_the_sample_draws_only_from_items_both_halves_rejected(kb):
    rejected = _add_item(kb, "Local sports result", h="H1")
    accepted = _add_item(kb, "Ukraine ceasefire", h="H2")
    kb.commit()
    comprehend.record_triage(kb, rejected, "immaterial", "none", "m", 1)
    comprehend.record_triage(kb, accepted, "material", "topical", "m", 1)
    kb.commit()

    assert comprehend.select_sampled(kb, 1, 10) == [rejected], (
        "a material item is already integrated; sampling it would not be a "
        "selection-independent control"
    )


def test_the_sample_respects_the_daily_cap(kb):
    ids = [_add_item(kb, f"Item {i}", h=f"H{i}") for i in range(5)]
    kb.commit()
    for i in ids:
        comprehend.record_triage(kb, i, "immaterial", "none", "m", 1)
    kb.commit()

    assert len(comprehend.select_sampled(kb, 1, 2)) == 2


def test_an_already_sampled_item_is_not_sampled_again(kb):
    item_id = _add_item(kb, "Local sports result")
    kb.commit()
    comprehend.record_triage(kb, item_id, "immaterial", "none", "m", 1)
    kb.commit()
    assert comprehend.select_sampled(kb, 1, 10) == [item_id]

    comprehend.record_triage(kb, item_id, "material", "sampled", None, 1)
    kb.commit()
    assert comprehend.select_sampled(kb, 1, 10) == []


def test_the_daily_cap_blocks_a_later_run_on_the_same_day(kb):
    """Pins the PER-DAY half of the budget, which no other test reaches.

    The existing cap test runs against a fresh schema, so `used` is 0 and a
    bare `LIMIT per_day` returns the same two rows -- it pins the LIMIT and
    not the day window. Here two rows are actually promoted to 'sampled'
    first, so the second call must come back empty on BUDGET grounds while a
    third immaterial item is still sitting there unselected.
    """
    ids = [_add_item(kb, f"Item {i}", h=f"H{i}") for i in range(3)]
    kb.commit()
    for i in ids:
        comprehend.record_triage(kb, i, "immaterial", "none", "m", 1)
    kb.commit()

    first = comprehend.select_sampled(kb, 1, 2)
    assert len(first) == 2, "the day starts with the full budget available"
    for i in first:
        comprehend.record_triage(kb, i, "material", "sampled", "m", 1)
        # The budget is spent at PROMOTION (F1): stamp sampled_at the same way
        # run()'s control-arm loop does, or this test cannot reach the cap at all.
        kb.execute(
            "UPDATE item_triage SET sampled_at = now() "
            "WHERE item_id = %s AND triage_prompt_version = 1",
            (i,),
        )
    kb.commit()

    assert comprehend.select_sampled(kb, 1, 2) == [], (
        "the day's budget of 2 is spent; a bare LIMIT would refill it on every "
        "fire and turn 20/day into 480/day on an hourly schedule"
    )
    assert len(comprehend.select_sampled(kb, 1, 3)) == 1, (
        "presence sibling: the empty result above must be the BUDGET, not an "
        "empty pool -- a third immaterial item was there the whole time, and "
        "without this the assertion above also passes for a sampler that can "
        "never return anything at all"
    )


def test_a_full_pass_triages_and_integrates_with_both_calls_stubbed(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    tracked = _add_item(kb, "Ukraine signals a ceasefire", h="Ht")
    topical = _add_item(kb, "Sahel coup attempt reported", h="Hp")
    kb.commit()

    def fake_triage(_req):
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": topical, "material": True}]},
                }
            ],
        }

    def fake_integrate(req):
        sent = [
            int(line.split("item_id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if "item_id=" in line
        ]
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": i,
                                "entities": [
                                    {"name": f"E{i}", "type": "country", "aliases": []}
                                ],
                                "events": [
                                    {
                                        "summary": f"S{i}",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                            for i in sent
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)
    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.triaged_by_rules == 1, "Ukraine matched the tracked half, no model"
    assert tally.triaged_by_model == 1, "Sahel needed the model half"
    assert tally.material == 2
    assert tally.assertions_written == 2
    reasons = dict(kb.execute("SELECT item_id, reason FROM item_triage").fetchall())
    assert reasons[tracked] == "tracked_entity"
    assert reasons[topical] == "topical"


def test_an_item_the_validator_rejects_is_charged_an_attempt(kb, monkeypatch):
    """Otherwise it is re-selected first on every pass, forever.

    A rejected row never reaches write_batch, so nothing sets integrated_at and
    nothing bumps integrate_attempts -- and the integration SELECT's
    `ORDER BY i.id` puts it at the FRONT of the next batch. It would re-pay an
    expensive call every pass while no counter moved.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    good = _add_item(kb, "Ukraine talks resume", h="Hg")
    bad = _add_item(kb, "Ukraine border incident", h="Hb")
    kb.commit()

    def fake_integrate(req):
        sent = [
            int(line.split("item_id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if "item_id=" in line
        ]
        items = []
        for i in sent:
            if i == good:
                items.append(
                    {
                        "item_id": i,
                        "entities": [
                            {
                                "name": "Kyiv delegation",
                                "type": "country",
                                "aliases": [],
                            }
                        ],
                        "events": [
                            {
                                "summary": "S",
                                "type": "action",
                                "commitment_state": "in_force",
                                "standing": "reported",
                            }
                        ],
                    }
                )
            else:
                # An entity carrying a candidate_id nothing offered: the
                # cheapest way to make _validate_item reject the whole row.
                items.append(
                    {
                        "item_id": i,
                        "entities": [{"candidate_id": 999999999}],
                        "events": [
                            {
                                "summary": "S",
                                "type": "action",
                                "commitment_state": "in_force",
                                "standing": "reported",
                            }
                        ],
                    }
                )
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {"items": items},
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    tally = comprehend.run(kb)
    kb.commit()

    attempts = dict(
        kb.execute("SELECT item_id, integrate_attempts FROM item_triage").fetchall()
    )
    assert attempts[bad] == 1, "the rejected item is now charged one attempt"
    assert attempts[good] == 0, (
        "presence sibling: without this, an implementation charging EVERY "
        "item in the batch also passes the assertion above"
    )
    assert (
        kb.execute(
            "SELECT integrated_at FROM item_triage WHERE item_id = %s", (good,)
        ).fetchone()[0]
        is not None
    ), "the accepted item actually integrated"
    assert tally.assertions_written == 1


def test_a_passed_deadline_stops_before_the_next_batch(kb, monkeypatch):
    """DEADLINE_SECONDS is 2400 and a test runs in milliseconds, so nothing
    here would ever reach the break naturally -- delete either one and the
    suite stays green while an overrunning hourly pass collides with its own
    next run.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    # The second half of this test marks the item immaterial via the model, and
    # the default per-day budget would then promote it into the sampled arm and
    # on to an unstubbed call_integration -- the exact gotcha Task 10's own
    # test_an_enabled_run_sees_the_item hit. Zero it so this test stays scoped
    # to the deadline.
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    item_id = _add_item(kb, "Storm hits coastal region")
    kb.commit()

    def fail_if_called(_req):
        pytest.fail("call_triage must not run once the deadline has passed")

    monkeypatch.setattr(comprehend, "call_triage", fail_if_called)
    monkeypatch.setattr(comprehend, "DEADLINE_SECONDS", 0)

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.items_seen >= 1
    assert tally.triaged_by_model == 0

    # Presence sibling, same fixture: with the deadline restored, the model
    # half IS reached -- otherwise `== 0` above would also pass for a run that
    # skips the loop for an unrelated reason.
    monkeypatch.setattr(comprehend, "DEADLINE_SECONDS", 2400)

    def fake_triage(_req):
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": item_id, "material": False}]},
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)

    tally2 = comprehend.run(kb)
    kb.commit()

    assert tally2.triaged_by_model == 1


def test_a_sampled_item_reaches_integration(kb, monkeypatch):
    """The sampled arm is the control the gate reads topical rows against. If
    sampled rows never integrate, the control is empty and the comparison is
    against nothing -- and the integration SELECT filters on verdict alone, so
    adding `AND t.reason != 'sampled'` to it currently fails no test.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    item_id = _add_item(kb, "Storm hits coastal region")
    kb.commit()

    def fake_triage(_req):
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": item_id, "material": False}]},
                }
            ],
        }

    def fake_integrate(req):
        sent = [
            int(line.split("item_id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if "item_id=" in line
        ]
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": i,
                                "entities": [
                                    {"name": f"E{i}", "type": "country", "aliases": []}
                                ],
                                "events": [
                                    {
                                        "summary": f"S{i}",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                            for i in sent
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)
    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    tally = comprehend.run(kb)
    kb.commit()

    row = kb.execute(
        "SELECT verdict, reason FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone()
    assert row == ("material", "sampled")
    assert tally.assertions_written == 1, (
        "the sampled item must reach integration, not merely promotion"
    )


def test_the_daily_cap_counts_promotions_not_row_creation(kb):
    """A row triaged YESTERDAY and promoted today must still spend today's
    budget. created_at records triage and ON CONFLICT never rewrites it, so
    counting it lets the cap silently unbind across the UTC day boundary --
    20 per fire on an hourly schedule is 480/day in the expensive tier.
    """
    ids = [_add_item(kb, f"Item {i}", h=f"H{i}") for i in range(3)]
    kb.commit()
    for i in ids:
        comprehend.record_triage(kb, i, "immaterial", "none", "m", 1)
    # Backdate created_at to simulate the state production reaches naturally
    # overnight: triaged yesterday, still sitting immaterial.
    kb.execute("UPDATE item_triage SET created_at = created_at - interval '1 day'")
    kb.commit()

    first = comprehend.select_sampled(kb, 1, 2)
    assert len(first) == 2, (
        "created_at is backdated but sampled_at is still NULL for every row -- "
        "the budget must read as fully available"
    )
    for i in first:
        comprehend.record_triage(kb, i, "material", "sampled", "m", 1)
        kb.execute(
            "UPDATE item_triage SET sampled_at = now() "
            "WHERE item_id = %s AND triage_prompt_version = 1",
            (i,),
        )
    kb.commit()

    assert comprehend.select_sampled(kb, 1, 2) == [], (
        "both were promoted TODAY (sampled_at = now()) even though created_at "
        "was backdated to yesterday -- a cap counting created_at would wrongly "
        "see yesterday's timestamps and refill the budget"
    )
    assert len(comprehend.select_sampled(kb, 1, 3)) == 1, (
        "presence sibling: a third immaterial item is still sitting there "
        "unselected -- the empty result above is the BUDGET, not an empty pool"
    )


def test_a_bumped_TRIAGE_prompt_version_is_integrated_on_its_own_row(kb, monkeypatch):
    """0009 splits the two versions so a triage bump and an integration bump are
    independent. With the integration SELECT joining on item_id alone, the new
    v2 row is offered every pass, the guard matches the OLD v1 row and no-ops,
    integrated_at is never set on v2, and integrate_attempts never moves --
    re-selected forever, paying a model call each time.
    """
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    item_id = _add_item(kb, "Ukraine talks resume")
    kb.commit()

    # Integrate at triage version 1 directly, no model call needed.
    comprehend.record_triage(kb, item_id, "material", "tracked_entity", None, 1)
    kb.commit()
    index = comprehend.SurfaceIndex.build(kb)
    tally = comprehend.Tally(enabled=True)
    comprehend.write_extraction(
        kb,
        {
            "item_id": item_id,
            "entities": [{"name": "Kyiv delegation", "type": "country", "aliases": []}],
            "events": [
                {
                    "summary": "S1",
                    "type": "action",
                    "commitment_state": "in_force",
                    "standing": "reported",
                }
            ],
        },
        index,
        tally,
    )
    kb.commit()
    v1_integrated_at = kb.execute(
        "SELECT integrated_at FROM item_triage "
        "WHERE item_id = %s AND triage_prompt_version = 1",
        (item_id,),
    ).fetchone()[0]
    assert v1_integrated_at is not None, "setup: v1 must actually be integrated"

    # Bump the triage version and record a fresh v2 triage row for the same item.
    monkeypatch.setattr(comprehend, "TRIAGE_PROMPT_VERSION", 2)
    comprehend.record_triage(kb, item_id, "material", "tracked_entity", None, 2)
    kb.commit()

    def fake_integrate(req):
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": item_id,
                                "entities": [
                                    {"name": "E2", "type": "country", "aliases": []}
                                ],
                                "events": [
                                    {
                                        "summary": "S2",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    comprehend.run(kb)
    kb.commit()

    v1_row = kb.execute(
        "SELECT integrated_at FROM item_triage "
        "WHERE item_id = %s AND triage_prompt_version = 1",
        (item_id,),
    ).fetchone()
    v2_row = kb.execute(
        "SELECT integrated_at, integrate_attempts FROM item_triage "
        "WHERE item_id = %s AND triage_prompt_version = 2",
        (item_id,),
    ).fetchone()
    assert v2_row[0] is not None, "the v2 row must be the one integration advances"
    assert v1_row[0] == v1_integrated_at, (
        "presence sibling: the v1 row must be untouched, not blanket-updated -- "
        "an implementation that stamps every version for this item_id also "
        "passes the assertion above"
    )
