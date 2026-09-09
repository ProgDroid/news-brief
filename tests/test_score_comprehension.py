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
from datetime import datetime, timedelta, timezone

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


# --- Windowed, exposure-capped corroboration (news-brief-bqa.19) -------------
#
# `corroboration_by_outlet` aggregates over the WHOLE KB, so a run spanning the
# candidate-ranking cutover (51c850c) blends two systems and attributes the
# result to neither -- the design spec calls the missing window argument a
# requirement, not a note. Two confounds have to die together:
#
#   1. the cutover itself -- events matched under recency ranking vs under
#      shared-entity ranking, and
#   2. EVENT AGE -- corroboration accrues as a second outlet's item arrives,
#      so a young post-cutover cohort compared against a mature pre-cutover one
#      understates the new ranking no matter how well it works.
#
# (2) is why a bare `--since` is not enough. Each event is scored over a FIXED
# exposure horizon measured from its own creation, and both cohorts are held to
# the same one, so age stops being a variable rather than being hoped away.

BASE = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _event_at(kb, created_at, type_="action", commitment_state="in_force"):
    """An event with an explicit `created_at`, which `_event` cannot set."""
    return kb.execute(
        "INSERT INTO events (summary, type, commitment_state, occurred_at, "
        "created_at) VALUES ('an event', %s, %s, %s, %s) RETURNING id",
        (type_, commitment_state, created_at, created_at),
    ).fetchone()[0]


def _assertion_at(kb, item_id, event_id, created_at, standing="reported"):
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (item_id, event_id, standing, created_at),
    )


# 9. A window scopes the denominator: an event created before `since` is not
# counted at all, however well corroborated it is.
def test_corroboration_window_excludes_events_created_outside_it(kb):
    old, new = BASE - timedelta(days=10), BASE - timedelta(days=1)
    e_old = _event_at(kb, old)
    _assertion_at(kb, _item(kb, outlet_name="Reuters"), e_old, old)
    _assertion_at(kb, _item(kb, outlet_name="AP"), e_old, old)
    e_new = _event_at(kb, new)
    _assertion_at(kb, _item(kb, outlet_name="AFP"), e_new, new)
    kb.commit()

    # Unscoped this KB reads 2 events, 1 multi-outlet. Scoped to the last two
    # days it must see only the uncorroborated one.
    rate, total, multi = sc.corroboration_by_outlet(kb, since=BASE - timedelta(days=2))
    assert (total, multi) == (1, 0)
    assert rate == 0.0


# 10. The exposure horizon is what makes two cohorts of different ages
# comparable: a second outlet arriving after it does not count.
def test_exposure_horizon_ignores_a_second_outlet_arriving_after_it(kb):
    born = BASE - timedelta(days=3)
    ev = _event_at(kb, born)
    _assertion_at(kb, _item(kb, outlet_name="Reuters"), ev, born)
    _assertion_at(kb, _item(kb, outlet_name="AP"), ev, born + timedelta(hours=20))
    kb.commit()

    _, total_capped, multi_capped = sc.corroboration_by_outlet(kb, horizon_hours=12)
    _, total_open, multi_open = sc.corroboration_by_outlet(kb, horizon_hours=24)
    # The positive control matters: a horizon that drops every assertion would
    # also report multi=0, and would be wrong for the opposite reason.
    assert (total_capped, multi_capped) == (1, 0)
    assert (total_open, multi_open) == (1, 1)


# 11. The pre-cutover control cohort spans exactly as long as the post-cutover
# one -- an unequal span reintroduces the size confound the horizon removes.
def test_cohorts_span_equal_lengths_on_both_sides_of_the_cutover(kb):
    cutover = BASE - timedelta(hours=36)
    report = sc.corroboration_cohorts(kb, cutover=cutover, horizon_hours=12, now=BASE)
    assert report["measurable"] is True
    pre_start, pre_end = report["pre_span"]
    post_start, post_end = report["post_span"]
    assert post_start == cutover
    assert post_end == BASE - timedelta(hours=12)
    assert pre_end == cutover
    assert (pre_end - pre_start) == (post_end - post_start)


# 12. The maturity guard: an event too young to have lived out the horizon is
# excluded, rather than counted as uncorroborated.
def test_cohorts_exclude_events_younger_than_the_exposure_horizon(kb):
    cutover = BASE - timedelta(hours=36)
    mature = BASE - timedelta(hours=24)
    young = BASE - timedelta(hours=2)
    for born in (mature, young):
        ev = _event_at(kb, born)
        _assertion_at(kb, _item(kb, outlet_name=f"outlet-{born.hour}"), ev, born)
    kb.commit()

    report = sc.corroboration_cohorts(kb, cutover=cutover, horizon_hours=12, now=BASE)
    _, total, _ = report["post"]
    assert total == 1


# 13. Too soon after the cutover is NOT MEASURABLE, never a rate of 0.0 --
# the same discipline score_match_rate_corroboration already keeps.
def test_cohorts_not_measurable_before_the_horizon_elapses(kb):
    report = sc.corroboration_cohorts(
        kb, cutover=BASE - timedelta(hours=4), horizon_hours=12, now=BASE
    )
    assert report["measurable"] is False
    assert report["post"] is None
    assert "horizon" in report["reason"]


# 14. The cutover anchor comes from the migration ledger, which the system
# wrote itself, rather than from a timestamp somebody remembered to record.
def test_deploy_anchor_is_the_latest_migrations_applied_time(kb):
    expected_version = db._available("up")[-1][0]
    version, applied_at = sc.deploy_anchor(kb)
    assert version == expected_version
    row = kb.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = %s", (version,)
    ).fetchone()
    assert applied_at == row[0]


# 15. Cohort mode is OBSERVATIONAL. The pre-registered gate is one-shot, so a
# windowed look must not render a verdict that spends it.
def test_sweep_mode_prints_no_gate_verdict(kb, capsys):
    sc.print_sweep(kb, cutover=BASE - timedelta(hours=36), horizons=[6, 12], now=BASE)
    out = capsys.readouterr().out
    assert "OBSERVATION" in out
    assert "PASS" not in out
    assert "FAIL" not in out
    assert "GATE" not in out


# 16. A naive --cutover is read as UTC. The column is timestamptz and the
# host's offset would otherwise shift the whole window silently, in the
# direction nobody checks.
def test_naive_cutover_is_read_as_utc():
    assert sc._parse_cutover("2026-09-08T18:00:00") == datetime(
        2026, 9, 8, 18, 0, tzinfo=timezone.utc
    )
    assert sc._parse_cutover("2026-09-08T18:00:00+02:00") == datetime(
        2026, 9, 8, 16, 0, tzinfo=timezone.utc
    )


# 17. The sweep reports one cohort comparison per horizon, in the order asked
# for, so the reader can see the parameter varying rather than trust one value.
def test_sweep_returns_one_row_per_horizon_in_order(kb):
    rows = sc.corroboration_sweep(
        kb, cutover=BASE - timedelta(hours=72), horizons=[6, 12, 24], now=BASE
    )
    assert [h for h, _ in rows] == [6, 12, 24]
    assert all(report["horizon_hours"] == h for h, report in rows)


# 18. The point of sweeping: the same KB reads as uncorroborated at one
# horizon and corroborated at another. A sweep that could not show this would
# be three restatements of one number.
def test_sweep_shows_a_gap_only_one_horizon_can_see(kb):
    born = BASE - timedelta(hours=48)
    ev = _event_at(kb, born)
    _assertion_at(kb, _item(kb, outlet_name="Reuters"), ev, born)
    _assertion_at(kb, _item(kb, outlet_name="AP"), ev, born + timedelta(hours=20))
    kb.commit()

    rows = dict(
        sc.corroboration_sweep(
            kb, cutover=BASE - timedelta(hours=72), horizons=[6, 24], now=BASE
        )
    )
    assert rows[6]["post"] == (0.0, 1, 0)
    assert rows[24]["post"] == (1.0, 1, 1)


# 19. The migration ledger dates MIGRATIONS, not commits. A change that ships
# without one cannot be dated from it at all, so the anchor is offered as a
# hint with the check spelled out -- and nothing is measured until it is
# answered. Guessing it wrong once already produced a confident wrong table.
def test_cohorts_mode_refuses_to_guess_the_cutover(kb, capsys):
    code = sc.main(["--cohorts"])
    out = capsys.readouterr().out
    assert code == 2
    assert "OBSERVATION" not in out
    assert "committed before" in out
    assert db._available("up")[-1][0] in out


# 20. A longer horizon yields a SHORTER span, strictly inside the shorter
# horizon's. The rows are nested samples of the same events, so agreement
# between them is close to one observation and must not read as two.
def test_sweep_names_the_nesting_between_horizons(kb, capsys):
    sc.print_sweep(kb, cutover=BASE - timedelta(hours=36), horizons=[6, 12], now=BASE)
    out = capsys.readouterr().out
    assert "NESTED, not independent" in out
    assert "12h" in out and "6h" in out
