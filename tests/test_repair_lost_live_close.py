"""Tests for the one-off repair of the live close the book lost (news-brief-sb0).

This script edits live financial state, so the tests that matter are the ones
that prove it REFUSES: a repair that quietly does the wrong thing to a book that
has moved on is worse than no repair at all.
"""

import json

import pytest

from scripts import repair_lost_live_close as repair

ROW_ID = repair.ROW_ID


def _row(**over):
    row = {
        "id": ROW_ID,
        "execution": "live",
        "status": "closed",
        "close_reason": "settled",
        "instrument": "3501950",
        "outcome": "No",
        "shares": 2.022221,
        "cost_basis": 2.0,
        "realized_return": None,
    }
    row.update(over)
    return row


def _book(*rows):
    return {"positions": list(rows)}


def _write(tmp_path, book):
    p = tmp_path / "book.json"
    p.write_text(json.dumps(book), encoding="utf-8")
    return p


def test_repair_computes_the_return_from_the_measured_proceeds():
    row = _row()
    repair.check(row)
    changes = repair.repair(row)
    assert changes["realized_return"] == pytest.approx(1.8033655465 / 2.0 - 1.0)
    assert changes["last_mark"]["price"] == 0.9165
    assert changes["last_mark"]["proceeds"] == 1.8033655465


def test_repair_restores_the_reason_the_close_actually_had():
    """reconcile_live_book stamped "settled", which says the venue disposed of
    the position by itself. It was sold, on request, by /close."""
    assert repair.repair(_row())["close_reason"] == "manual"


def test_refuses_a_row_that_was_already_repaired():
    """If realized_return is set, something else filled it -- backfill_settled,
    or an earlier run of this script. Overwriting would silently replace a
    number nobody asked about."""
    with pytest.raises(repair.Refused, match="already"):
        repair.check(_row(realized_return=-0.1))


def test_refuses_a_row_the_venue_never_settled():
    with pytest.raises(repair.Refused, match="close_reason"):
        repair.check(_row(close_reason="manual"))


def test_refuses_when_the_book_and_the_venue_disagree_on_size():
    """The proceeds are for 2.022221 shares. Against a row holding a different
    number they are not this row's proceeds, and the return would be fiction."""
    with pytest.raises(repair.Refused, match="cannot be attributed"):
        repair.check(_row(shares=5.0))


def test_refuses_a_missing_row(tmp_path):
    p = _write(tmp_path, _book(_row(id="something-else")))
    assert repair.main([str(p)]) == 2


def test_refuses_a_zero_cost_basis():
    with pytest.raises(repair.Refused, match="cost_basis"):
        repair.check(_row(cost_basis=0))


def test_dry_run_writes_nothing(tmp_path):
    p = _write(tmp_path, _book(_row()))
    before = p.read_text(encoding="utf-8")
    assert repair.main([str(p)]) == 0
    assert p.read_text(encoding="utf-8") == before


def test_apply_writes_the_repair_and_keeps_a_backup(tmp_path):
    p = _write(tmp_path, _book(_row()))
    assert repair.main([str(p), "--apply"]) == 0
    got = json.loads(p.read_text(encoding="utf-8"))["positions"][0]
    assert got["realized_return"] == pytest.approx(1.8033655465 / 2.0 - 1.0)
    assert got["close_reason"] == "manual"
    assert got["last_mark"]["proceeds"] == 1.8033655465
    assert list(tmp_path.glob("book.*.bak")), "a repair of live state keeps a backup"


def test_apply_leaves_every_other_row_untouched(tmp_path):
    other = {"id": "2026-09-02:prediction:1:YES:live", "status": "open"}
    p = _write(tmp_path, _book(_row(), other))
    assert repair.main([str(p), "--apply"]) == 0
    rows = json.loads(p.read_text(encoding="utf-8"))["positions"]
    assert rows[1] == other


def test_running_it_twice_refuses_the_second_time(tmp_path):
    """The guard that makes this safe to hand to an operator."""
    p = _write(tmp_path, _book(_row()))
    assert repair.main([str(p), "--apply"]) == 0
    assert repair.main([str(p), "--apply"]) == 2
