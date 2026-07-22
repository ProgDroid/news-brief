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


def test_prompt_block_requires_min_sample():
    # 4 trades < n>=5 floor → empty
    book = {"positions": [_closed("equity", 0.1, confidence="high") for _ in range(4)]}
    assert validation.performance_prompt_block(book) == ""


def test_prompt_block_includes_qualifying_dimension():
    book = {
        "positions": [
            _closed("equity", 0.1, edge=0.03, confidence="high") for _ in range(6)
        ]
    }
    out = validation.performance_prompt_block(book)
    assert out  # non-empty
    assert "high" in out or "equity" in out


def test_aggregate_includes_source_dimensions():
    book = {
        "positions": [
            dict(
                _closed("equity", 0.10, edge=0.04),
                source_kind="regional",
                source_perspective="ARAB",
            ),
            dict(
                _closed("equity", -0.05, edge=-0.02),
                source_kind="wire",
                source_perspective=None,
            ),
        ]
    }
    agg = validation.aggregate_performance(book)
    assert agg["dimensions"]["source_kind"]["regional"]["n"] == 1
    assert agg["dimensions"]["source_kind"]["wire"]["n"] == 1
    assert agg["dimensions"]["source_perspective"]["ARAB"]["n"] == 1


def test_fmt_marks_thin_samples():
    thin = {
        "n": 1,
        "hit_rate": 100.0,
        "mean_net": 0.1,
        "median_net": 0.1,
        "mean_edge": 0.05,
        "n_edge": 1,
    }
    assert "thin" in validation._fmt(thin)
    fat = dict(thin, n=validation._REPORT_MIN_N)
    assert "thin" not in validation._fmt(fat)


def test_calibration_block_flags_inversion():
    # high realizes LESS edge than medium -> inverted
    book = {
        "positions": [
            *[
                dict(_closed("equity", 0.02, edge=0.05), confidence="medium")
                for _ in range(3)
            ],
            *[
                dict(_closed("equity", 0.01, edge=0.01), confidence="high")
                for _ in range(3)
            ],
        ]
    }
    agg = validation.aggregate_performance(book)
    lines = validation._calibration_block(agg)
    joined = "\n".join(lines)
    assert "Calibration" in joined
    assert "inverted" in joined.lower()


def test_calibration_block_silent_when_monotonic():
    book = {
        "positions": [
            *[
                dict(_closed("equity", 0.01, edge=0.01), confidence="medium")
                for _ in range(3)
            ],
            *[
                dict(_closed("equity", 0.05, edge=0.06), confidence="high")
                for _ in range(3)
            ],
        ]
    }
    agg = validation.aggregate_performance(book)
    joined = "\n".join(validation._calibration_block(agg))
    assert "inverted" not in joined.lower()


def test_calibration_block_empty_without_confidence_data():
    assert validation._calibration_block({"dimensions": {}, "overall": None}) == []


def test_daily_trade_message_empty_when_nothing():
    assert validation.daily_trade_message({"positions": []}, "2026-06-14") == ""


def test_daily_trade_message_opened_and_open():
    book = {
        "positions": [
            {
                "status": "open",
                "opened": "2026-06-14",
                "asset_class": "equity",
                "ticker": "SHEL",
                "direction": "bullish",
                "play_type": None,
                "entry_price": 30.0,
                "last_mark": None,
            },
            {
                "status": "open",
                "opened": "2026-06-14",
                "asset_class": "prediction",
                "ticker": "mkt1",
                "direction": "bullish",
                "play_type": "momentum",
                "outcome": "Yes",
                "entry_price": 0.4,
                "last_mark": None,
                "rationale": "matched (similarity=0.7)",
            },
            {
                "status": "open",
                "opened": "2026-05-01",
                "asset_class": "crypto",
                "ticker": "BTC",
                "direction": "bullish",
                "play_type": None,
                "entry_price": 60000.0,
                "last_mark": {"date": "2026-06-08", "price": 66000.0, "return": 0.10},
            },
        ]
    }
    out = validation.daily_trade_message(book, "2026-06-14")
    assert "SHEL" in out  # opened today
    assert "mkt1" in out  # prediction suggestion
    assert "BTC" in out  # open-positions summary
    assert "+10" in out  # last-known mark for the older open position


def test_leakage_summary_sums_recent_window(monkeypatch, tmp_path):
    f = tmp_path / "leak.json"
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", f)
    validation._write_json_atomic(
        f,
        {
            "2026-06-20": {"traded": 1, "no_ticker": 2},
            "2026-06-21": {"traded": 3, "no_ticker": 1, "no_price": 1},
        },
    )
    totals = validation.leakage_summary(window_days=7)
    assert totals["traded"] == 4
    assert totals["no_ticker"] == 3
    assert totals["no_price"] == 1


def test_leakage_summary_respects_window(monkeypatch, tmp_path):
    f = tmp_path / "leak.json"
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", f)
    validation._write_json_atomic(
        f,
        {
            "2026-06-01": {"traded": 99},
            "2026-06-24": {"traded": 1},
            "2026-06-25": {"traded": 2},
        },
    )
    assert validation.leakage_summary(window_days=2)["traded"] == 3


def test_leakage_block_empty_when_no_log(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", tmp_path / "absent.json")
    assert validation._leakage_block() == []


def test_aggregate_performance_excludes_live():
    # net_return is the field _stats scores on; a live row carrying it would be
    # counted but for the execution!="live" guard (paper gate stays paper-only).
    book = {
        "positions": [
            {
                "status": "closed",
                "asset_class": "prediction",
                "execution": "paper",
                "net_return": 0.05,
            },
            {
                "status": "closed",
                "asset_class": "prediction",
                "execution": "live",
                "sleeve": "A",
                "net_return": -0.9,
            },
        ]
    }
    agg = validation.aggregate_performance(book)
    assert agg["overall"]["n"] == 1  # live row excluded from the paper gate


def test_live_performance_reports_live_only():
    book = {
        "positions": [
            {
                "status": "closed",
                "execution": "live",
                "sleeve": "A",
                "realized_return": 0.1,
            },
            {
                "status": "closed",
                "execution": "live",
                "sleeve": "A",
                "realized_return": -0.2,
            },
            {"status": "closed", "execution": "paper", "realized_return": 0.5},
        ]
    }
    lp = validation.live_performance(book)
    assert lp["n"] == 2 and abs(lp["mean_return"] - (-0.05)) < 1e-9
