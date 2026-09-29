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
