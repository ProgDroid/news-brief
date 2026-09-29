"""Pure rules of the event census: the block window and the draw order.

No database -- these exercise `census.compute_block`, `census.exclusion_reason`
and `census.draw_order` directly against synthetic `WindowStat` values.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import census
import census_metrics
import comprehend


def test_block_on_2026_10_01_is_ten_days_from_09_18():
    block = census.compute_block(date(2026, 10, 1))
    assert block.start == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert block.end == datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert (block.end - block.start).days == 10


def test_block_on_2026_09_30_refuses_naming_2026_10_01():
    with pytest.raises(census.CensusRefusal, match="2026-10-01"):
        census.compute_block(date(2026, 9, 30))


def test_block_later_is_fourteen_days():
    block = census.compute_block(date(2026, 10, 20))
    assert block.start == datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert block.end == datetime(2026, 10, 17, tzinfo=timezone.utc)
    assert (block.end - block.start).days == census.BLOCK_DAYS


def test_lookback_matches_comprehend():
    assert census.LOOKBACK_DAYS == comprehend.CANDIDATE_WINDOW_DAYS


@pytest.mark.parametrize(
    "title,published_delta,expected",
    [
        ("  ", None, "empty_title"),
        ("A real headline", timedelta(hours=25), "backlog"),
        ("A real headline", timedelta(hours=23), None),
        ("A real headline", None, None),
        # The boundary itself (M4): spec says "more than 24h", so exactly 24h
        # is kept and a `>` -> `>=` mutation at census.py's backlog check
        # flips this case and fails.
        ("A real headline", timedelta(hours=24), None),
        ("A real headline", timedelta(hours=24, seconds=1), "backlog"),
    ],
)
def test_exclusion_reasons(title, published_delta, expected):
    created_at = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    published_at = None if published_delta is None else created_at - published_delta
    assert census.exclusion_reason(title, published_at, created_at) == expected


def test_quote_pages_are_excluded_through_common(monkeypatch):
    created_at = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    assert census.exclusion_reason("LCO - Reuters", None, created_at) == "quote_page"

    import common

    monkeypatch.setattr(common, "is_quote_page", lambda t: False)
    assert census.exclusion_reason("LCO - Reuters", None, created_at) is None


def _synthetic_windows(counts: dict[int, int]) -> list[census.WindowStat]:
    """`counts` maps stratum -> how many windows to synthesize for it, each
    one day apart (within a stratum) so `sorted(by start)` is deterministic
    and every window's start is distinct (M6: this previously said "ten
    minutes apart", which was never true of the `timedelta(days=i)` below)."""
    windows = []
    for stratum, n in counts.items():
        base = datetime(2026, 9, 18, stratum * 6, 0, tzinfo=timezone.utc)
        for i in range(n):
            windows.append(
                census.WindowStat(
                    start=base + timedelta(days=i),
                    stratum=stratum,
                    item_ids=(i,),
                    backlog_excluded=0,
                    null_published=0,
                )
            )
    return windows


def test_draw_order_two_per_stratum_in_first_eight():
    windows = _synthetic_windows({0: 10, 1: 10, 2: 10, 3: 10})
    order = census.draw_order(windows)
    assert len(order) == 16
    assert len({w.start for w in order}) == 16

    first_eight = order[:8]
    strata = [w.stratum for w in first_eight]
    for s in range(4):
        assert strata.count(s) == 2


def test_draw_order_is_deterministic():
    windows = _synthetic_windows({0: 10, 1: 10, 2: 10, 3: 10})
    first = census.draw_order(windows)
    second = census.draw_order(windows)
    assert [w.start for w in first] == [w.start for w in second]


def test_draw_order_refuses_a_thin_stratum():
    windows = _synthetic_windows({0: 10, 1: 10, 2: 10, 3: 3})
    with pytest.raises(census.CensusRefusal, match="stratum 3"):
        census.draw_order(windows)


def test_draw_order_matches_the_golden_sequence_for_seed():
    """M8: pins the exact order_no -> (stratum, window_start) draw for SEED
    on the standard synthetic input (computed once by actually running
    `draw_order` against it, not hand-derived), so a refactor of the
    interleaving -- e.g. collapsing the per-round fresh
    `rng.sample([0, 1, 2, 3], 4)` into one permutation drawn once, which
    plan ruling R8 says is also a valid reading of the brief -- cannot
    silently change the already-frozen draw without failing a test."""
    windows = _synthetic_windows({0: 10, 1: 10, 2: 10, 3: 10})
    order = census.draw_order(windows)

    expected = [
        (0, "2026-09-26T00:00:00+00:00"),
        (3, "2026-09-18T18:00:00+00:00"),
        (2, "2026-09-21T12:00:00+00:00"),
        (1, "2026-09-19T06:00:00+00:00"),
        (0, "2026-09-22T00:00:00+00:00"),
        (3, "2026-09-22T18:00:00+00:00"),
        (1, "2026-09-25T06:00:00+00:00"),
        (2, "2026-09-23T12:00:00+00:00"),
        (2, "2026-09-18T12:00:00+00:00"),
        (0, "2026-09-18T00:00:00+00:00"),
        (3, "2026-09-24T18:00:00+00:00"),
        (1, "2026-09-23T06:00:00+00:00"),
        (1, "2026-09-22T06:00:00+00:00"),
        (0, "2026-09-19T00:00:00+00:00"),
        (2, "2026-09-27T12:00:00+00:00"),
        (3, "2026-09-23T18:00:00+00:00"),
    ]
    got = [(w.stratum, w.start.isoformat()) for w in order]
    assert got == expected


def test_session_constants_are_the_plan_pinned_values():
    assert (
        census.GO_MIN_MEAN_GROUPS,
        census.GO_MAX_MEDIAN_MINUTES,
        census.PRECISION_PAIRS,
        census.IDLE_CAP_MINUTES,
        census.REPEAT_AFTER_ORDER_NO,
        census.REPEAT_MIN_DAYS,
        census.TOTAL_SESSIONS,
    ) == (8, 80, 10, 5, 8, 7, 17)


def test_go_thresholds_match_census_metrics():
    """`census_metrics.go_no_go` takes no threshold parameters, so pin
    census's named thresholds to it at both boundaries: exactly on each
    passes, one step past either fails."""
    at_floor = [census.GO_MIN_MEAN_GROUPS] * 2
    at_ceiling = [float(census.GO_MAX_MEDIAN_MINUTES)] * 2
    assert census_metrics.go_no_go(at_floor, at_ceiling).ok is True
    below = [census.GO_MIN_MEAN_GROUPS, census.GO_MIN_MEAN_GROUPS - 1]
    assert census_metrics.go_no_go(below, at_ceiling).ok is False
    over = [census.GO_MAX_MEDIAN_MINUTES + 0.01] * 2
    assert census_metrics.go_no_go(at_floor, over).ok is False
