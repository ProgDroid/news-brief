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


# ── Prediction rows read as questions, not market ids ─────────────────────────
# A prediction row has no ticker: trading.py stores the market id in `ticker` and
# the question in `topic`, so the old message showed only "2774056".


def _pred_row(**kw):
    row = {
        "status": "open",
        "opened": "2026-08-05",
        "asset_class": "prediction",
        "execution": "paper",
        "ticker": "2774056",
        "instrument": "2774056",
        "direction": "bullish",
        "play_type": "momentum",
        "outcome": "Yes",
        "topic": "Will Iran & Israel agree a ceasefire before October?",
        "entry_price": 0.41,
        "last_mark": None,
    }
    row.update(kw)
    return row


def test_daily_trade_message_names_prediction_markets():
    out = validation.daily_trade_message({"positions": [_pred_row()]}, "2026-08-05")
    assert "Will Iran &amp; Israel agree a ceasefire" in out  # the question, escaped
    assert "<code>2774056</code>" in out  # id kept as the /close handle
    assert "&amp;" in out and " & " not in out  # bare & would 400 the whole message


def test_daily_trade_message_truncates_long_questions():
    long_q = "Will " + "x" * 300 + "?"
    out = validation.daily_trade_message(
        {"positions": [_pred_row(topic=long_q)]}, "2026-08-05"
    )
    assert "…" in out
    assert max(len(ln) for ln in out.splitlines()) < 120


def test_daily_trade_message_falls_back_to_id_without_question():
    out = validation.daily_trade_message(
        {"positions": [_pred_row(topic=None)]}, "2026-08-05"
    )
    assert "2774056" in out  # renderable even when the market fetch lost the question


def test_daily_trade_message_separates_live_from_paper():
    book = {
        "positions": [
            _pred_row(),
            _pred_row(
                execution="live",
                sleeve="A",
                topic="Fed cuts rates in September?",
                ticker="991",
                instrument="991",
                cost_basis=2.0,
                entry_price=0.83,
                play_type="resolution",
            ),
        ]
    }
    out = validation.daily_trade_message(book, "2026-08-05")
    assert "Prediction suggestions (paper)" in out
    assert "real money" in out  # live rows get their own, unmistakable section
    assert "$2 @ 0.83" in out
    # The two are otherwise identical on the wire; the tag is the only signal.
    assert "💵 live" in out and "[paper]" in out


# ── Sleeve A status block: why the live sleeve did nothing ────────────────────


def test_sleeve_a_block_reports_flags_when_off():
    out = validation.daily_trade_message(
        {"positions": []},
        "2026-08-05",
        {"state": "off", "live_enabled": True, "a_enabled": False},
    )
    # A message is emitted even with an empty book — "are the flags actually set in
    # the container" is the first question a zero-trade day raises.
    assert "PG_LIVE_ENABLED=1" in out and "PG_A_ENABLED=0" in out


def test_sleeve_a_block_flags_faults_but_not_design_declines():
    status = {
        "state": "ran",
        "candidates": 9,
        "matches": 4,
        "opened": 0,
        "wallet": 12.4,
        "skips": {"out_of_band": 3, "spread_too_wide": 1, "book_unreadable": 2},
        "blocked": [{"question": "Fed cuts?", "price": 0.97, "why": "out_of_band"}],
    }
    out = validation.daily_trade_message({"positions": []}, "2026-08-05", status)
    assert "4 match(es) → 0 opened" in out and "wallet $12.40" in out
    assert "price outside band ×3" in out
    assert "orderbook unreadable ×2 ⚠️" in out  # a fault, marked
    # An illiquid book is the gate WORKING. Marking it ⚠️ alongside a failing venue
    # read is what made a healthy 2026-08-09 run look broken.
    assert "spread too wide ×1" in out
    assert "spread too wide ×1 ⚠️" not in out
    assert "price outside band ×3 ⚠️" not in out  # design working, not marked
    assert "Fed cuts? @ 0.97" in out  # the number, so "missed by a cent" is visible


def test_sleeve_a_block_marks_unreadable_wallet():
    status = {"state": "ran", "matches": 1, "opened": 0, "wallet": None, "skips": {}}
    out = validation.daily_trade_message({"positions": []}, "2026-08-05", status)
    # cap_ok fail-closes on an unreadable balance, so this alone explains zero orders.
    assert "wallet UNREADABLE ⚠️" in out


def test_daily_trade_message_still_empty_without_status_or_positions():
    assert validation.daily_trade_message({"positions": []}, "2026-08-05") == ""


def test_sleeve_a_block_reports_a_crash():
    """A swallowed exception must not read as "the sleeve declined every market"."""
    out = validation.daily_trade_message(
        {"positions": []},
        "2026-08-05",
        {"state": "crashed", "error": "TypeError: bad <thing>"},
    )
    assert "CRASHED" in out and "TypeError: bad &lt;thing&gt;" in out
