"""Pure-function tests for census_metrics.py (spec sec 4.4-4.6, 6.4).

No I/O, no database: every fixture here is hand-built. See task-2-brief.md
for the exact values these tests pin.
"""

import math
import random
from datetime import datetime, timedelta

import pytest

from census_metrics import (
    Assignment,
    T80,
    T975,
    active_minutes,
    adjusted_rand_index,
    bcubed_recall,
    confirms,
    deciles,
    draw_precision_sample,
    gap_band,
    go_no_go,
    groups,
    item_bootstrap_interval,
    item_bootstrap_intervals,
    mde,
    multi_outlet_groups,
    pair_recall,
    pairwise_agreement,
    pooled_share,
    precision_estimate,
    split_share,
)


def test_untouched_items_are_singletons():
    assignments = [
        Assignment(item_id=1, outlet_id=1, group_id=None, unsure=False),
        Assignment(item_id=2, outlet_id=2, group_id=None, unsure=False),
        Assignment(item_id=3, outlet_id=3, group_id=None, unsure=False),
        Assignment(item_id=4, outlet_id=1, group_id=None, unsure=False),
    ]

    g = groups(assignments)

    assert sorted(g, key=min) == [
        frozenset({1}),
        frozenset({2}),
        frozenset({3}),
        frozenset({4}),
    ]
    assert multi_outlet_groups(assignments) == []


def test_unsure_item_is_removed_and_group_rescored():
    # outlets {1, 1, 2}: items 10, 11 on outlet 1; item 12 on outlet 2.
    base = [
        Assignment(item_id=10, outlet_id=1, group_id=1, unsure=False),
        Assignment(item_id=11, outlet_id=1, group_id=1, unsure=False),
        Assignment(item_id=12, outlet_id=2, group_id=1, unsure=False),
    ]

    # Case A: the outlet-2 item is unsure -> the surviving group is single-outlet.
    case_a = list(base)
    case_a[2] = Assignment(item_id=12, outlet_id=2, group_id=1, unsure=True)
    assert multi_outlet_groups(case_a) == []

    # Case B: one outlet-1 item is unsure -> a 2-item multi-outlet group remains.
    case_b = list(base)
    case_b[0] = Assignment(item_id=10, outlet_id=1, group_id=1, unsure=True)
    mog = multi_outlet_groups(case_b)
    assert mog == [frozenset({11, 12})]
    assert len(mog[0]) == 2


def test_confirms_needs_two_outlets_in_one_event():
    outlet_of = {"a": 1, "b": 2}
    assert confirms(frozenset({"a", "b"}), outlet_of, {"a": {7}, "b": {7}}) is True
    assert confirms(frozenset({"a", "b"}), outlet_of, {"a": {7}, "b": {8}}) is False

    same_outlet = {"a": 1, "b": 1}
    assert confirms(frozenset({"a", "b"}), same_outlet, {"a": {7}, "b": {7}}) is False


def test_pooled_share_weighs_groups_not_windows():
    outlet_of = {}
    events_of = {}
    window_a = []
    for i in range(4):
        a, b = f"a{i}", f"b{i}"
        outlet_of[a], outlet_of[b] = 1, 2
        events_of[a] = events_of[b] = {100 + i}
        window_a.append(frozenset({a, b}))

    outlet_of["x"], outlet_of["y"] = 1, 2
    events_of["x"], events_of["y"] = {90}, {91}
    window_b = [frozenset({"x", "y"})]

    share = pooled_share([window_a, window_b], outlet_of, events_of)

    assert share == pytest.approx(0.8)


def test_pooled_share_is_nan_with_no_multi_outlet_groups():
    assert math.isnan(pooled_share([[], []], {}, {}))


def test_pair_and_bcubed_recall_on_a_hand_partition():
    outlet_of = {"a": 1, "b": 2, "c": 3}
    events_of = {"a": {100}, "b": {100}, "c": {200}}
    windows = [[frozenset({"a", "b", "c"})]]

    assert pair_recall(windows, outlet_of, events_of) == pytest.approx(1 / 3)
    assert bcubed_recall(windows, outlet_of, events_of) == pytest.approx(5 / 9)


def test_recall_is_not_transitive():
    # x-y share event 1, y-z share event 2, but x-z share nothing: a system
    # that links x-y and y-z has NOT linked x-z (R7 ruling).
    outlet_of = {"x": 1, "y": 2, "z": 3}
    events_of = {"x": {1}, "y": {1, 2}, "z": {2}}
    windows = [[frozenset({"x", "y", "z"})]]

    assert pair_recall(windows, outlet_of, events_of) == pytest.approx(2 / 3)
    assert bcubed_recall(windows, outlet_of, events_of) == pytest.approx(
        (2 / 3 + 1 + 2 / 3) / 3
    )


def test_bcubed_recall_same_outlet_pair_is_not_linked_via_bridge():
    # p and q are on the SAME outlet and share no event with each other, but
    # each shares a distinct event with r (a different outlet). Direct-link
    # bcubed must not credit p and q as linked to each other through that
    # bridge -- if it did (transitive closure), the recall below would be
    # 1.0 (a perfect cluster) instead of 7/9.
    outlet_of = {"p": 1, "q": 1, "r": 2}
    events_of = {"p": {5}, "q": {6}, "r": {5, 6}}
    windows = [[frozenset({"p", "q", "r"})]]

    recall = bcubed_recall(windows, outlet_of, events_of)

    assert recall == pytest.approx(7 / 9)
    assert recall != pytest.approx(1.0)


def test_pairwise_agreement_and_ari():
    ref = {1: 1, 2: 1, 3: 2, 4: 2}
    other_identical = dict(ref)

    precision, recall, f1 = pairwise_agreement(ref, other_identical)
    assert (precision, recall, f1) == (
        pytest.approx(1),
        pytest.approx(1),
        pytest.approx(1),
    )
    assert adjusted_rand_index(ref, other_identical) == pytest.approx(1)

    other = {1: 1, 2: 1, 3: 1, 4: 1}
    precision, recall, f1 = pairwise_agreement(ref, other)
    assert precision == pytest.approx(2 / 6)
    assert recall == pytest.approx(1)
    assert adjusted_rand_index(ref, other) < 1


def test_all_singletons_are_not_one_cluster():
    ref = {1: None, 2: None, 3: None}
    other = dict(ref)

    assert adjusted_rand_index(ref, other) == pytest.approx(1)

    precision, _recall, _f1 = pairwise_agreement({1: None, 2: None}, {1: 5, 2: 5})
    assert precision == pytest.approx(0)


def test_mde_reproduces_the_spec_table():
    cases = [
        ([8] * 16, (21.8, 24.4, 33.0)),
        ([4, 12] * 8, (22.6, 25.8, 36.0)),
        ([10] * 16, (20.2, 23.1, 32.2)),
        ([6] * 16, (24.2, 26.5, 34.2)),
    ]
    for m_values, (at_05, at_10, at_30) in cases:
        assert mde(m_values, 0.05) == pytest.approx(at_05, abs=0.15)
        assert mde(m_values, 0.1) == pytest.approx(at_10, abs=0.15)
        assert mde(m_values, 0.3) == pytest.approx(at_30, abs=0.15)


def test_mde_uses_t_for_the_window_count():
    assert mde([10] * 8, 0.1) == pytest.approx(35.5, abs=0.15)


def test_mde_rejects_fewer_than_two_windows():
    with pytest.raises(ValueError):
        mde([8], 0.1)


def test_go_no_go_thresholds():
    result = go_no_go([8, 8], [80, 80])
    assert result.ok is True

    result = go_no_go([8, 7], [10, 10])
    assert result.ok is False
    assert "mean" in result.reason

    result = go_no_go([9, 9], [81, 81])
    assert result.ok is False
    assert "minutes" in result.reason


def test_gap_bands_at_the_boundaries():
    assert gap_band(0.399) == "proceed"
    assert gap_band(0.40) == "within_6h_only"
    assert gap_band(0.50) == "within_6h_only"
    assert gap_band(0.501) == "stop"


def test_split_share_caps_each_gap_at_one():
    assert split_share([3, 12]) == pytest.approx(0.75)


def test_precision_sample_is_one_pair_per_group_without_replacement():
    def make_group(gid, size, outlets):
        return frozenset(f"g{gid}i{i}" for i in range(size)), {
            f"g{gid}i{i}": outlets[i % len(outlets)] for i in range(size)
        }

    outlet_of = {}
    g1, o1 = make_group(1, 2, [1, 2])
    g2, o2 = make_group(2, 2, [1, 2])
    g3, o3 = make_group(3, 6, [1, 2, 3, 4, 5, 6])
    outlet_of.update(o1)
    outlet_of.update(o2)
    outlet_of.update(o3)
    groups3 = [g1, g2, g3]

    rng = random.Random(1)
    sample = draw_precision_sample(groups3, outlet_of, rng, n=10)

    assert len(sample) == 3
    seen_groups = set()
    for a, b in sample:
        assert a < b
        assert outlet_of[a] != outlet_of[b]
        owner = next(g for g in groups3 if a in g and b in g)
        seen_groups.add(owner)
    assert len(seen_groups) == 3

    # 12 groups, n=10 -> 10 pairs from 10 distinct groups.
    many_groups = []
    outlet_of2 = {}
    for gid in range(12):
        g, o = make_group(gid, 2, [1, 2])
        many_groups.append(g)
        outlet_of2.update(o)
    rng2 = random.Random(2)
    sample2 = draw_precision_sample(many_groups, outlet_of2, rng2, n=10)
    assert len(sample2) == 10
    owners = set()
    for a, b in sample2:
        owners.add(next(g for g in many_groups if a in g and b in g))
    assert len(owners) == 10


def test_precision_sample_draws_groups_at_equal_probability():
    six_outlet = frozenset(f"s{i}" for i in range(6))
    two_outlet = frozenset({"t0", "t1"})
    outlet_of = {f"s{i}": i for i in range(6)}
    outlet_of.update({"t0": 100, "t1": 101})
    groups2 = [six_outlet, two_outlet]

    six_count = 0
    trials = 2000
    for seed in range(trials):
        rng = random.Random(seed)
        sample = draw_precision_sample(groups2, outlet_of, rng, n=1)
        assert len(sample) == 1
        a, b = sample[0]
        if a in six_outlet or b in six_outlet:
            six_count += 1

    share = six_count / trials
    assert share == pytest.approx(0.5, abs=0.04)


def test_precision_estimate_is_pooled_over_groups():
    p, _lo, _hi = precision_estimate([(9, 10), (1, 2)])
    assert p == pytest.approx(10 / 12)
    assert p != pytest.approx(0.7)


def test_precision_estimate_below_two_windows_has_no_interval():
    p, lo, hi = precision_estimate([(5, 10)])
    assert p == pytest.approx(0.5)
    assert lo is None
    assert hi is None


def test_active_minutes_drops_idle_gaps():
    base = datetime(2026, 9, 28, 0, 0, 0)
    stamps = [base + timedelta(minutes=m) for m in (0, 1, 2, 10, 11)]

    assert active_minutes(stamps) == pytest.approx(3.0)


def test_t_tables_cover_df_1_to_15():
    assert set(T975) == set(range(1, 16))
    assert set(T80) == set(range(1, 16))
    assert T975[1] == pytest.approx(12.706)
    assert T80[1] == pytest.approx(1.376)
    assert T975[15] == pytest.approx(2.131)
    assert T80[15] == pytest.approx(0.866)


def test_deciles_returns_nine_cut_points():
    values = list(range(1, 101))
    d = deciles([float(v) for v in values])
    assert len(d) == 9
    assert d == tuple(sorted(d))


def test_item_bootstrap_interval_varies_and_is_seed_stable():
    # ref != other, so the bootstrap distribution is non-degenerate: a
    # broken resample (e.g. not actually sampling with replacement) or a
    # broken percentile calculation would show up as a collapsed or shifted
    # interval, unlike the ref == other case this replaces (constant 1.0 for
    # every replicate regardless of whether the resample logic runs at all).
    ref = {i: i % 3 for i in range(20)}
    other = {i: (i // 2) % 3 for i in range(20)}

    point = adjusted_rand_index(ref, other)
    lo, hi, dropped = item_bootstrap_interval(
        adjusted_rand_index, ref, other, reps=300, seed=7
    )

    assert lo < hi
    assert lo - 1e-9 <= point <= hi + 1e-9

    lo2, hi2, dropped2 = item_bootstrap_interval(
        adjusted_rand_index, ref, other, reps=300, seed=7
    )
    assert (lo2, hi2, dropped2) == (lo, hi, dropped)


def test_item_bootstrap_interval_drops_nan_replicates():
    # 30 items, pass 1 has 3 two-item groups and pass 2 agrees on 2 and adds
    # one: some resamples contain no same-cluster pair in pass 2, so pairwise
    # precision is NaN there. Sorting those in used to scramble the interval.
    ref = {i: (i // 2 if i < 6 else None) for i in range(30)}
    other = {
        i: (i // 2 if i < 4 else (7 if i in (10, 11) else None)) for i in range(30)
    }

    def precision(x, y):
        return pairwise_agreement(x, y)[0]

    point = precision(ref, other)
    lo, hi, dropped = item_bootstrap_interval(precision, ref, other, reps=2000)
    assert dropped > 0
    assert lo <= hi
    assert lo - 1e-9 <= point <= hi + 1e-9


def test_item_bootstrap_interval_gives_up_when_too_few_replicates_survive():
    ref = {i: None for i in range(10)}  # all singletons: recall is always NaN

    def recall(x, y):
        return pairwise_agreement(x, y)[1]

    assert item_bootstrap_interval(recall, ref, ref, reps=200) == (None, None, 200)


# ── Task 2 (census minors): ARI, F1, precision, constants, bootstrap ────────


def _partitions(n: int):
    """Every set partition of range(n) as a list of block labels (restricted
    growth strings), so each partition appears exactly once."""

    def grow(prefix: list[int], top: int):
        if len(prefix) == n:
            yield list(prefix)
            return
        for label in range(top + 1):
            yield from grow([*prefix, label], max(top, label + 1))

    yield from grow([], 0)


def test_ari_denominator_is_never_zero_past_the_special_cases():
    """The old `denom == 0` branch was dead: over every pair of partitions of
    n <= 5 items, the general formula's denominator is 0 exactly when both
    labellings are all-singleton or both are one cluster -- the cases that
    return 1.0 before it -- and never otherwise."""

    def c2(k):
        return k * (k - 1) // 2

    checked = special = 0
    for n in range(1, 6):
        parts = list(_partitions(n))
        for pa in parts:
            for pb in parts:
                a = dict(enumerate(pa))
                b = dict(enumerate(pb))
                sizes_a = [pa.count(k) for k in set(pa)]
                sizes_b = [pb.count(k) for k in set(pb)]
                sum_a = sum(c2(s) for s in sizes_a)
                sum_b = sum(c2(s) for s in sizes_b)
                expected = sum_a * sum_b / c2(n) if c2(n) else 0.0
                denom = 0.5 * (sum_a + sum_b) - expected
                is_special = (len(sizes_a) == len(sizes_b) == 1) or (
                    len(sizes_a) == len(sizes_b) == n
                )
                value = adjusted_rand_index(a, b)
                if is_special:
                    special += 1
                    assert value == 1.0
                else:
                    assert denom != 0, (pa, pb)
                    assert not math.isnan(value), (pa, pb)
                checked += 1
    assert checked == sum(len(list(_partitions(n))) ** 2 for n in range(1, 6))
    assert special > 0


def test_ari_special_cases_and_empty_input():
    # both all-singleton (None items): identical by uniqueness, so 1.0
    assert (
        adjusted_rand_index({1: None, 2: None, 3: None}, {1: None, 2: None, 3: None})
        == 1.0
    )
    # both one cluster: likewise 1.0
    assert adjusted_rand_index({1: 7, 2: 7, 3: 7}, {1: 9, 2: 9, 3: 9}) == 1.0
    # no shared items: nan (unlike scikit-learn's 1.0) so bootstrap drops it
    assert math.isnan(adjusted_rand_index({}, {}))
    assert math.isnan(adjusted_rand_index({1: 1}, {2: 1}))


def test_pairwise_f1_with_precision_and_recall_both_fractional():
    ref = {1: 1, 2: 1, 3: 1, 4: 2}  # same-pairs 12 13 23
    other = {1: 1, 2: 1, 3: 2, 4: 2}  # same-pairs 12 34
    precision, recall, f1 = pairwise_agreement(ref, other)
    assert precision == pytest.approx(1 / 2)
    assert recall == pytest.approx(1 / 3)
    assert f1 == pytest.approx(2 / 5)
    assert f1 != pytest.approx((precision + recall) / 2)


def test_precision_estimate_with_nothing_asked_is_undefined_not_an_error():
    p, lo, hi = precision_estimate([(0, 0), (0, 0)])
    assert math.isnan(p)
    assert lo is None and hi is None
    p, lo, hi = precision_estimate([(0, 0)])
    assert math.isnan(p) and lo is None and hi is None


def test_thresholds_are_named_constants_used_as_defaults():
    import inspect

    import census_metrics

    assert (
        census_metrics.GO_MIN_MEAN_GROUPS,
        census_metrics.GO_MAX_MEDIAN_MINUTES,
        census_metrics.PRECISION_PAIRS,
        census_metrics.IDLE_CAP_MINUTES,
    ) == (8, 80, 10, 5)

    def default(fn, name):
        return inspect.signature(fn).parameters[name].default

    assert default(go_no_go, "min_mean_groups") == census_metrics.GO_MIN_MEAN_GROUPS
    assert (
        default(go_no_go, "max_median_minutes") == census_metrics.GO_MAX_MEDIAN_MINUTES
    )
    assert default(draw_precision_sample, "n") == census_metrics.PRECISION_PAIRS
    assert (
        default(active_minutes, "idle_cap_minutes") == census_metrics.IDLE_CAP_MINUTES
    )


def test_census_reexports_the_constants_rather_than_redefining_them():
    import re
    from pathlib import Path

    import census
    import census_metrics

    for name in (
        "GO_MIN_MEAN_GROUPS",
        "GO_MAX_MEDIAN_MINUTES",
        "PRECISION_PAIRS",
        "IDLE_CAP_MINUTES",
    ):
        assert getattr(census, name) == getattr(census_metrics, name)
        source = Path(census.__file__).read_text(encoding="utf-8")
        assert not re.search(rf"^{name}\s*=", source, re.M), name


_BOOT_A = {i: (i // 3 if i % 7 else None) for i in range(24)}
_BOOT_B = {i: (i // 4 if i % 5 else None) for i in range(24)}
_SPARSE_A = {0: 1, 1: 1, 2: None, 3: None, 4: None}
_SPARSE_B = {0: 1, 1: None, 2: 2, 3: 2, 4: None}


def _four_stats(x, y):
    return (*pairwise_agreement(x, y), adjusted_rand_index(x, y))


def test_bootstrap_intervals_are_pinned_on_a_fixed_fixture():
    """Values captured from the one-bootstrap-per-statistic implementation
    before the refactor to a single shared pass (reps=300)."""
    got = item_bootstrap_intervals(_four_stats, _BOOT_A, _BOOT_B, reps=300)
    assert got == [
        (0.23752941176470588, 0.7710664335664335, 0),
        (0.22884375, 0.8926923076923073, 0),
        (0.25252659574468084, 0.7690451388888884, 0),
        (0.18638702059921144, 0.7438163157460305, 0),
    ]
    got = item_bootstrap_intervals(_four_stats, _SPARSE_A, _SPARSE_B, reps=300)
    assert got == [
        (0.0, 1.0, 67),
        (0.0, 1.0, 97),
        (None, None, 228),
        (-0.17647058823529413, 1.0, 0),
    ]
    # the single-statistic form still agrees with the old per-stat results
    assert item_bootstrap_interval(
        adjusted_rand_index, _SPARSE_A, _SPARSE_B, reps=300
    ) == (-0.17647058823529413, 1.0, 0)


def test_bootstrap_scores_pairwise_agreement_once_per_replicate(monkeypatch):
    import census_metrics
    from scripts import census_report

    calls = []
    real = census_metrics.pairwise_agreement

    def counting(x, y):
        calls.append(1)
        return real(x, y)

    monkeypatch.setattr(census_metrics, "pairwise_agreement", counting)
    rows = census_report._consistency_rows(_BOOT_A, _BOOT_B, 50)
    assert len(calls) == 50 + 1  # one per replicate plus the point value
    assert [r[0] for r in rows] == [
        "pairwise precision",
        "pairwise recall",
        "pairwise F1",
        "ARI",
    ]
    assert rows[0][1] == pytest.approx(0.2857142857142857)
    assert rows[1][1] == pytest.approx(0.375)
