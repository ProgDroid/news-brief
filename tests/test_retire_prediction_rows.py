"""Tests for the one-off retirement of open paper prediction rows (news-brief-oh4).

polygram.ink was dropped as a venue on 2026-09-11. The paper prediction sleeve
took its prices from the same API, so its open rows can never be marked again.
Decided by the operator: they close at their last known mark, reason
venue_retired, are SCORED as if that mark were the close (mirroring the retired
_stamp_close_metrics prediction branch), and are KEPT.
"""

import json

import pytest

from scripts import retire_prediction_rows as retire


def _pred(mid, *, status="open", last_mark=None, execution="paper", **over):
    row = {
        "id": f"2026-08-20:prediction:{mid}:YES:{execution}",
        "asset_class": "prediction",
        "execution": execution,
        "status": status,
        "instrument": mid,
        "ticker": mid,
        "direction": "bullish",
        "play_type": "resolution",
        "entry_price": 0.4,
        "entry_spread": 0.015,
        "last_mark": last_mark,
        "close_reason": None,
        "closed_date": None,
        "realized_return": None,
    }
    row.update(over)
    return row


def _book():
    return {
        "positions": [
            _pred("m1", last_mark={"date": "2026-09-05", "price": 0.5, "return": 0.25}),
            _pred("m2"),  # never marked
            _pred(
                "m3", status="closed", close_reason="settlement", realized_return=1.0
            ),
            _pred("m4", execution="live", status="closed", close_reason="settled"),
            _pred(
                "m5",
                play_type="momentum",
                entry_spread=None,
                last_mark={"date": "2026-09-05", "price": 0.1, "return": -0.99},
            ),
            {"id": "eq", "asset_class": "equity", "status": "open", "ticker": "SHEL"},
        ]
    }


def _plan(book, today="2026-09-12"):
    return {row["instrument"]: ch for row, ch in retire.plan(book, today=today)}


def test_open_paper_prediction_rows_close_and_score_at_their_last_mark():
    ch = _plan(_book())["m1"]
    assert ch["status"] == "closed" and ch["close_reason"] == "venue_retired"
    assert ch["closed_date"] == "2026-09-12"
    assert ch["realized_return"] == 0.25
    assert ch["haircut"] == 0.015  # the orderbook half-spread captured at open
    assert abs(ch["net_return"] - 0.235) < 1e-12
    assert ch["benchmark_return"] == 0.0 and abs(ch["edge"] - 0.235) < 1e-12


def test_scoring_mirrors_the_retired_branch_for_momentum_and_the_floor():
    """momentum pays a second 200bps leg; no entry_spread falls back to the 200bps
    default; and a stake cannot lose more than itself."""
    ch = _plan(_book())["m5"]
    assert abs(ch["haircut"] - 0.04) < 1e-12  # 0.02 default + 0.02 momentum exit leg
    assert ch["net_return"] == -1.0  # -0.99 - 0.04 floored


def test_a_row_never_marked_retires_unscored():
    ch = _plan(_book())["m2"]
    assert ch["status"] == "closed" and ch["close_reason"] == "venue_retired"
    for k in ("realized_return", "haircut", "net_return", "benchmark_return", "edge"):
        assert ch[k] is None, k


def test_closed_rows_live_rows_and_other_classes_are_untouched():
    touched = set(_plan(_book()))
    assert touched == {"m1", "m2", "m5"}


def test_refuses_while_a_live_row_is_still_open():
    book = _book()
    book["positions"].append(_pred("m9", execution="live"))
    with pytest.raises(retire.Refused, match="live"):
        retire.plan(book, today="2026-09-12")


def test_nothing_to_do_is_an_empty_plan_not_a_refusal():
    book = {"positions": [{"id": "eq", "asset_class": "equity", "status": "open"}]}
    assert retire.plan(book, today="2026-09-12") == []


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert retire.main([str(path)]) == 0
    assert path.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "3 row(s) to retire, 1 never marked" in out


def test_apply_writes_and_a_second_run_finds_nothing(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    assert retire.main([str(path), "--apply"]) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    m1 = next(p for p in after["positions"] if p.get("instrument") == "m1")
    assert m1["status"] == "closed" and abs(m1["net_return"] - 0.235) < 1e-12
    assert list(tmp_path.glob("book.*.bak"))
    assert retire.plan(after, today="2026-09-12") == []
