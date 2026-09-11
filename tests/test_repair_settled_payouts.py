"""Tests for the one-off valuation of the six live rows the venue resolved (news-brief-9tq).

Six positions resolved at the venue and paid out shares x $1.00 -- six credits of
~$2.02 in the wallet history, read by the operator 2026-09-11. A resolution is not
an order, so /trade/history could never have valued them. This script writes the
return from the measured payout, and refuses a book that does not match the six.
"""

import json

import pytest

from scripts import repair_settled_payouts as repair

_RESOLVED = [
    ("2026-08-10:prediction:3324624:NO:live", 0.943, 2.021737),
    ("2026-08-10:prediction:2910437:NO:live", 0.89175, 2.022987),
    ("2026-08-11:prediction:3348047:NO:live", 0.779, 2.026312),
    ("2026-08-15:prediction:3399428:NO:live", 0.9225, 2.02222),
    ("2026-08-15:prediction:2910438:NO:live", 0.8405, 2.024389),
    ("2026-08-16:prediction:3491476:NO:live", 0.80975, 2.025315),
]


def _row(rid, fill, shares, **over):
    """A live row AFTER repair_live_cost_basis has run (fill_price present, cost 2.06)."""
    row = {
        "id": rid,
        "execution": "live",
        "sleeve": "A",
        "status": "closed",
        "close_reason": "settled",
        "closed_date": "2026-08-30",
        "instrument": rid.split(":")[2],
        "outcome": "No",
        "entry_price": 2.0 / shares,
        "fill_price": fill,
        "shares": shares,
        "cost_basis": 2.06,
        "realized_return": None,
        "last_mark": None,
    }
    row.update(over)
    return row


def _book(**over_by_market):
    rows = [
        _row(rid, fill, shares, **over_by_market.get(rid.split(":")[2], {}))
        for rid, fill, shares in _RESOLVED
    ]
    # The three SOLD rows, already valued -- must be left alone.
    rows.append(
        _row(
            "2026-09-01:prediction:3501950:NO:live",
            0.9225,
            2.022221,
            close_reason="manual",
            realized_return=-0.12458,
        )
    )
    rows.append(
        _row(
            "2026-08-11:prediction:2774057:NO:live",
            0.8815,
            2.02325,
            realized_return=-0.085884,
        )
    )
    rows.append(
        _row(
            "2026-09-01:prediction:2243896:NO:live",
            0.943,
            2.021737,
            close_reason="stop",
            realized_return=-0.163071,
        )
    )
    rows.append({"id": "paper", "execution": "paper"})
    return {"positions": rows}


def test_each_resolved_row_is_valued_at_shares_times_one_dollar():
    changes = repair.plan(_book())
    assert len(changes) == 6
    by_id = {row["id"]: ch for row, ch in changes}
    for rid, _fill, shares in _RESOLVED:
        ch = by_id[rid]
        assert abs(ch["realized_return"] - (shares / 2.06 - 1)) < 1e-12
        assert ch["last_mark"] == {
            "date": "2026-08-30",
            "price": 1.0,
            "proceeds": shares,
        }
    # A WIN at resolution still loses: shares x $1 against $2.06 paid.
    assert all(-0.02 < ch["realized_return"] < -0.015 for ch in by_id.values())


def test_sold_rows_and_paper_rows_are_untouched():
    touched = {row["id"] for row, _ in repair.plan(_book())}
    assert "2026-09-01:prediction:3501950:NO:live" not in touched
    assert "2026-08-11:prediction:2774057:NO:live" not in touched
    assert "paper" not in touched


def test_refuses_before_the_cost_basis_repair_has_run():
    book = _book()
    for p in book["positions"]:
        p.pop("fill_price", None)
        if p.get("execution") == "live":
            p["cost_basis"] = 2.0
    with pytest.raises(repair.Refused, match="repair_live_cost_basis"):
        repair.plan(book)


def test_refuses_when_the_resolved_set_is_not_the_measured_six():
    book = _book(**{"2910438": {"realized_return": -0.01}})  # one already valued
    with pytest.raises(repair.Refused, match="6"):
        repair.plan(book)


def test_refuses_a_settled_row_it_was_not_told_about():
    book = _book()
    book["positions"].append(_row("2026-09-12:prediction:999:NO:live", 0.9, 2.0228))
    with pytest.raises(repair.Refused, match="999"):
        repair.plan(book)


def test_refuses_a_share_count_that_would_not_pay_about_two_dollars():
    book = _book()
    next(p for p in book["positions"] if p.get("instrument") == "3348047")["shares"] = (
        3.5
    )
    with pytest.raises(repair.Refused, match="shares"):
        repair.plan(book)


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert repair.main([str(path)]) == 0
    assert path.read_text(encoding="utf-8") == before
    assert "6 row" in capsys.readouterr().out


def test_apply_writes_then_refuses_a_second_run(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(_book()), encoding="utf-8")
    assert repair.main([str(path), "--apply"]) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    row = next(p for p in after["positions"] if p.get("instrument") == "3348047")
    assert abs(row["realized_return"] - (2.026312 / 2.06 - 1)) < 1e-12
    assert row["last_mark"]["proceeds"] == 2.026312
    assert list(tmp_path.glob("book.*.bak"))
    assert repair.main([str(path), "--apply"]) == 2
