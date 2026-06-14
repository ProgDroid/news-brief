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


def _many(asset_class, n, net, edge):
    return [_closed(asset_class, net, edge=edge) for _ in range(n)]


def test_gate_not_ready_too_few_trades(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    book = {"positions": _many("equity", 5, 0.10, 0.05)}
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is False
    assert "closed" in res["equity"]["reason"]


def test_gate_ready_when_all_criteria_met(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    # seed two positive prior evals for the sustained-window check
    validation._write_json_atomic(hist, {"equity": [0.03, 0.04]})
    book = {"positions": _many("equity", 30, 0.10, 0.05)}  # all wins, edge +
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is True


def test_gate_not_ready_when_not_sustained(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    validation._write_json_atomic(hist, {"equity": [-0.01, 0.04]})  # one negative
    book = {"positions": _many("equity", 30, 0.10, 0.05)}
    res = validation.evaluate_gate(book)
    assert res["equity"]["ready"] is False
    assert "sustained" in res["equity"]["reason"] or "evals" in res["equity"]["reason"]


def test_record_gate_history_appends(monkeypatch, tmp_path):
    hist = tmp_path / "g.json"
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", hist)
    book = {"positions": _many("crypto", 3, 0.20, 0.10)}
    validation.record_gate_history(book)
    data = validation._load_json_or(hist, {})
    assert len(data["crypto"]) == 1
    assert abs(data["crypto"][0] - 0.10) < 1e-9
    assert data["equity"] == [None]  # no equity trades → null entry


def test_performance_report_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    book = {
        "positions": [
            _closed("equity", 0.10, edge=0.04, confidence="high", thesis_ref="oil"),
            _closed("equity", -0.05, edge=-0.02, confidence="medium", thesis_ref="oil"),
            _closed("crypto", 0.20, edge=0.10, confidence="high"),
        ]
    }
    out = validation.performance_report(book)
    assert "PERFORMANCE" in out
    assert "equity" in out
    assert "Go-live" in out or "go-live" in out.lower()


def test_performance_report_empty_book(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "GATE_HISTORY_FILE", tmp_path / "g.json")
    out = validation.performance_report({"positions": []})
    assert "No closed" in out or "no closed" in out.lower()
