import validation


def _closed(
    asset_class, net, edge=None, confidence=None, play_type=None, thesis_ref=None
):
    return {
        "status": "closed",
        "asset_class": asset_class,
        "net_return": net,
        "edge": edge,
        "confidence": confidence,
        "play_type": play_type,
        "thesis_ref": thesis_ref,
    }


def test_aggregate_overall_hit_rate_and_means():
    book = {
        "positions": [
            _closed("equity", 0.10, edge=0.04),
            _closed("equity", -0.05, edge=-0.02),
            _closed("crypto", 0.20, edge=0.10),
        ]
    }
    agg = validation.aggregate_performance(book)
    o = agg["overall"]
    assert o["n"] == 3
    assert abs(o["hit_rate"] - (100.0 * 2 / 3)) < 1e-9
    assert abs(o["mean_net"] - (0.25 / 3)) < 1e-9
    assert o["median_net"] == 0.10
    assert abs(o["mean_edge"] - (0.12 / 3)) < 1e-9


def test_aggregate_by_dimension():
    book = {
        "positions": [
            _closed("equity", 0.10, confidence="high"),
            _closed("equity", -0.05, confidence="medium"),
            _closed("crypto", 0.20, confidence="high"),
        ]
    }
    agg = validation.aggregate_performance(book)
    assert agg["dimensions"]["asset_class"]["equity"]["n"] == 2
    assert agg["dimensions"]["asset_class"]["crypto"]["n"] == 1
    assert agg["dimensions"]["confidence"]["high"]["n"] == 2


def test_aggregate_excludes_no_net_return():
    # pre-Phase-4 closed positions have no net_return → excluded entirely
    book = {
        "positions": [
            {"status": "closed", "asset_class": "equity"},  # legacy, no net_return
            _closed("equity", 0.10),
        ]
    }
    assert validation.aggregate_performance(book)["overall"]["n"] == 1


def test_aggregate_empty_book():
    assert validation.aggregate_performance({"positions": []})["overall"] is None
