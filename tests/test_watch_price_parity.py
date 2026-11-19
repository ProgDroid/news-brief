"""The pre-deploy parity comparison (news-brief-vap): ours vs watch_check.py."""

from scripts.watch_price_parity import compare


def test_all_agree():
    out = compare({"p1": "fired", "p2": "not"}, {"p1": "fired", "p2": "not"}, set())
    assert out == {"agree": 2, "disagree": [], "tolerated": [], "unknown": 0}


def test_disagreement_is_listed():
    out = compare({"p1": "fired"}, {"p1": "not"}, set())
    assert out["disagree"] == ["p1"] and out["agree"] == 0


def test_straddling_ma_row_is_tolerated_not_disagreeing():
    out = compare({"p1": "fired"}, {"p1": "not"}, {"p1"})
    assert out["tolerated"] == ["p1"] and out["disagree"] == []


def test_unknown_on_either_side_never_counts_as_agree():
    out = compare(
        {"p1": "unknown", "p2": "fired", "p3": "unknown"},
        {"p1": "fired", "p2": "unknown", "p3": "unknown"},
        set(),
    )
    assert out["unknown"] == 3 and out["agree"] == 0 and out["disagree"] == []


def test_a_row_missing_on_one_side_is_a_disagreement():
    out = compare({"p1": "fired"}, {}, set())
    assert out["disagree"] == ["p1"]
