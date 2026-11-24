"""Pure tests for grouping.decide_group (storylines plan 1, task 4).

No database: the DB-backed half of grouping is in tests/test_group_store.py.
"""

from datetime import date

import group_store as gs
import grouping

TH = grouping.Thresholds(join_sim=0.85, pair_sim=0.7, title_sim=0.5)


def _c(gid, sim, tsim=0.0):
    return gs.Candidate(group_id=gid, similarity=sim, title_similarity=tsim)


def test_decide_group_join_alone():
    best = grouping.decide_group([_c(7, 0.9, 0.0)], TH)
    assert best is not None and best.group_id == 7
    # The control: just under join, with no title support, does not join.
    assert grouping.decide_group([_c(7, 0.84, 0.0)], TH) is None


def test_decide_group_pair_rule():
    assert grouping.decide_group([_c(3, 0.75, 0.6)], TH).group_id == 3
    assert grouping.decide_group([_c(3, 0.75, 0.4)], TH) is None
    # The pair rule needs its own similarity floor too, not only the title.
    assert grouping.decide_group([_c(3, 0.69, 0.99)], TH) is None


def test_decide_group_prefers_highest_similarity_then_lowest_id():
    cands = [_c(5, 0.88), _c(2, 0.95), _c(9, 0.95), _c(1, 0.80, 0.9)]
    assert grouping.decide_group(cands, TH).group_id == 2
    # Order of the input does not matter.
    assert grouping.decide_group(list(reversed(cands)), TH).group_id == 2
    # A failing candidate with a higher similarity never wins over a passing one.
    th = grouping.Thresholds(join_sim=0.97, pair_sim=0.7, title_sim=0.5)
    cands = [_c(4, 0.96, 0.1), _c(8, 0.75, 0.6)]
    assert grouping.decide_group(cands, th).group_id == 8


def test_decide_group_none_when_nothing_passes():
    assert grouping.decide_group([], TH) is None
    assert grouping.decide_group([_c(1, 0.5, 0.9), _c(2, 0.8, 0.1)], TH) is None


def test_thresholds_come_from_a_calibration():
    cal = gs.Calibration(
        space="a@2",
        join_sim=0.9,
        pair_sim=0.75,
        title_sim=0.4,
        retrieval_min_sim=None,
        weighted_precision=0.9,
        weighted_recall=0.8,
        pairs_labelled=100,
        prompt_version=1,
        judge_model="m",
        week_ending=date(2026, 11, 22),
    )
    assert grouping.Thresholds.from_calibration(cal) == grouping.Thresholds(
        join_sim=0.9, pair_sim=0.75, title_sim=0.4
    )
