"""scripts/score_comprehension.py: the pre-registered gate's own logic.

The gate's stdout is the artifact the whole comprehension-pipeline build gets
judged by (spec section 8), and it is mostly SQL joins, aggregation and
boundary logic -- not something a threshold-only script can skip testing.
Two confirmed defects (an events/assertions join that only worked for
`assertions`, and a corroboration cross-check comparing two counts that were
equal by construction) shipped without a single test here catching them.

Fixture style follows tests/test_comprehend_integration.py: a `kb` fixture
that drops and re-migrates the public schema, so each test starts from a
clean, empty-but-migrated database.
"""

import math

import pytest

import db
from scripts import score_comprehension as sc

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


def _outlet(kb, name):
    row = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') "
        "ON CONFLICT DO NOTHING RETURNING id",
        (name,),
    ).fetchone()
    if row is None:
        row = kb.execute(
            "SELECT id FROM outlets WHERE lower(name) = lower(%s)", (name,)
        ).fetchone()
    return row[0]


_item_seq = iter(range(10**9))


def _item(kb, outlet_name="Reuters", reason="topical"):
    """A material item, triaged with `reason`, on its own fresh outlet+url."""
    n = next(_item_seq)
    oid = _outlet(kb, outlet_name)
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, %s, 'title', %s) RETURNING id",
        (oid, f"u{n}", f"h{n}"),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', %s, 1)",
        (iid, reason),
    )
    return iid


def _event(kb, type_="action", commitment_state="in_force"):
    return kb.execute(
        "INSERT INTO events (summary, type, commitment_state, occurred_at) "
        "VALUES ('an event', %s, %s, now()) RETURNING id",
        (type_, commitment_state),
    ).fetchone()[0]


def _assertion(kb, item_id, event_id, standing="reported"):
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) VALUES (%s, %s, %s)",
        (item_id, event_id, standing),
    )


def _corroboration_fixture(kb, events_n, matches_n):
    """`events_n` events, each with one creating assertion, plus `matches_n`
    additional assertions on the first event from fresh items -- exactly the
    shape score_match_rate_corroboration derives its rate from:
    matches = count(assertions) - count(events)."""
    events = [_event(kb) for _ in range(events_n)]
    for ev in events:
        _assertion(kb, _item(kb, outlet_name=f"base-{ev}"), ev)
    for i in range(matches_n):
        _assertion(kb, _item(kb, outlet_name=f"match-{i}"), events[0])
    kb.commit()


def _matches_for_rate(events_n, rate):
    """Smallest non-negative `matches` with matches/(events_n+matches) >= rate,
    derived from the real CORROBORATION_FLOOR rather than a copied number."""
    return max(0, math.ceil(rate * events_n / (1 - rate)))


# 1. An empty KB fails, and the failure names each unmeasured check rather
# than reporting a bare number.
def test_empty_kb_fails_naming_each_unmeasured_check(kb):
    failures = sc.run_gate(kb)
    assert failures
    for f in failures:
        # every failure is a descriptive phrase, not a bare number rendered
        # as a string
        assert not f.replace(".", "").replace(":", "").replace(" ", "").isdigit()
    assert "events.type" in failures
    assert "events.commitment_state" in failures
    assert "assertions.standing" in failures
    assert "corroboration: no events" in failures
    assert "match-rate corroboration: no assertions" in failures


# 2. A KB with events but ZERO assertions fails corroboration as
# not-measurable, not as 0.0.
def test_events_with_no_assertions_is_not_measurable(kb):
    _event(kb)
    kb.commit()
    ok, measured, detail = sc.score_match_rate_corroboration(kb)
    assert measured is False
    assert ok is False
    assert detail == "no assertions to measure"
    assert "0.0%" not in detail


# 3. An enum with a single distinct value fails the variance check
# (degenerate).
def test_single_distinct_value_fails_variance(kb):
    ev = _event(kb)
    _assertion(kb, _item(kb), ev, standing="reported")
    _assertion(kb, _item(kb, outlet_name="AP"), ev, standing="reported")
    kb.commit()
    shares, total = sc.distribution(kb, "assertions", "standing")
    ok, _ = sc.score_enum(shares, total)
    assert ok is False


# 4. An enum with a healthy spread passes it.
def test_healthy_spread_passes_variance(kb):
    # `standing`'s CHECK constraint allows 5 values; split evenly across
    # MIN_DISTINCT_AT_10PCT of them so both the distinct-count floor and the
    # single-value ceiling are satisfied by construction, not by a copied
    # number.
    standings = ["verified", "official", "reported", "attributed", "alleged"]
    n_values = max(2, sc.MIN_DISTINCT_AT_10PCT)
    chosen = standings[:n_values]
    for standing in chosen:
        for _ in range(4):
            ev = _event(kb)
            _assertion(kb, _item(kb), ev, standing=standing)
    kb.commit()
    shares, total = sc.distribution(kb, "assertions", "standing")
    ok, _ = sc.score_enum(shares, total)
    assert ok is True


# 5. Corroboration below §8.2's floor fails.
def test_corroboration_below_floor_fails(kb):
    events_n = 10
    at_floor = _matches_for_rate(events_n, sc.CORROBORATION_FLOOR)
    _corroboration_fixture(kb, events_n, max(0, at_floor - 1))
    ok, measured, _ = sc.score_match_rate_corroboration(kb)
    assert measured is True
    assert ok is False


# 6. Corroboration at or above the floor passes.
def test_corroboration_at_or_above_floor_passes(kb):
    events_n = 10
    at_floor = _matches_for_rate(events_n, sc.CORROBORATION_FLOOR)
    _corroboration_fixture(kb, events_n, at_floor)
    ok, measured, _ = sc.score_match_rate_corroboration(kb)
    assert measured is True
    assert ok is True


# 7. With no outlet at 10+ assertions, the within-outlet check reports
# not-measurable rather than printing nothing.
def test_no_outlet_at_threshold_reports_not_measurable(kb):
    ev = _event(kb)
    for _ in range(5):
        _assertion(kb, _item(kb, outlet_name="SmallOutlet"), ev)
    kb.commit()
    rows, measured = sc.standing_variance_by_outlet(kb)
    assert measured is False
    assert rows == []


# 8. The per-arm breakdown includes `tracked_claim` and covers all three
# enums.
def test_per_arm_breakdown_includes_tracked_claim_and_all_enums(kb):
    ev = _event(kb)
    it = _item(kb, reason="tracked_claim")
    _assertion(kb, it, ev, standing="reported")
    kb.commit()
    breakdown = sc.per_arm_breakdown(kb)
    assert set(breakdown.keys()) == set(sc.ENUMS)
    for key in sc.ENUMS:
        assert "tracked_claim" in breakdown[key]
        _, detail = breakdown[key]["tracked_claim"]
        assert detail != "no rows"
