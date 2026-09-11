"""Tests for the one-off re-basing of every live row (news-brief-rhg).

The script rewrites entry_price and cost_basis on nine rows of real-money state
and the realized_return of three completed round trips. The tests that matter
are the refusals: a book that does not match the nine-row world this repair was
reasoned about must be left alone.
"""

import json

import pytest

from scripts import repair_live_cost_basis as repair

# Nine live rows, as the 2026-08-29 book copy plus the two 2026-09-01 opens showed
# them. shares = 2 + 0.0205/fill on every one (the measured venue rule).
_LEGACY = [
    ("2026-08-10:prediction:3324624:NO:live", 0.943, 2.021737),
    ("2026-08-10:prediction:2910437:NO:live", 0.89175, 2.022987),
    ("2026-08-11:prediction:2774057:NO:live", 0.8815, 2.02325),
    ("2026-08-11:prediction:3348047:NO:live", 0.779, 2.026312),
    ("2026-08-15:prediction:3399428:NO:live", 0.9225, 2.02222),
    ("2026-08-15:prediction:2910438:NO:live", 0.8405, 2.024389),
    ("2026-08-16:prediction:3491476:NO:live", 0.80975, 2.025315),
    ("2026-09-01:prediction:3501950:NO:live", 0.9224999999999999, 2.022221),
    ("2026-09-01:prediction:2243896:NO:live", 0.943, 2.021737),
]


def _row(rid, fill, shares, **over):
    row = {
        "id": rid,
        "execution": "live",
        "sleeve": "A",
        "status": "closed",
        "close_reason": "settled",
        "instrument": rid.split(":")[2],
        "outcome": "No",
        "entry_price": fill,
        "shares": shares,
        "cost_basis": 2.0,
        "fees": {"spread_fee": 0.05, "trade_fee": 0.01, "total_fee": 0.06},
        "realized_return": None,
        "last_mark": None,
    }
    row.update(over)
    return row


def _book(**over_by_market):
    rows = []
    for rid, fill, shares in _LEGACY:
        rows.append(
            _row(rid, fill, shares, **over_by_market.get(rid.split(":")[2], {}))
        )
    rows.append(
        {"id": "paper", "execution": "paper", "entry_price": 0.5, "cost_basis": 2.0}
    )
    return {"positions": rows}


def _host_book():
    """The host book as of 2026-09-11: 3501950 repaired by hand (proceeds on
    last_mark), 2243896 closed natively (same), 2774057 backfilled from history
    (return only, no proceeds stored -- the pre-7f0a904 backfill)."""
    return _book(
        **{
            "3501950": {
                "close_reason": "manual",
                "realized_return": 1.8033655465 / 2.0 - 1,
                "last_mark": {
                    "date": "2026-09-10",
                    "price": 0.9165,
                    "proceeds": 1.8033655465,
                },
            },
            "2243896": {
                "close_reason": "stop",
                "realized_return": 1.7240742175 / 2.0 - 1,
                "last_mark": {
                    "date": "2026-09-10",
                    "price": 0.8775,
                    "proceeds": 1.7240742175,
                },
            },
            "2774057": {"realized_return": 1.88307871875 / 2.0 - 1},
        }
    )


def _write(tmp_path, book):
    p = tmp_path / "book.json"
    p.write_text(json.dumps(book), encoding="utf-8")
    return p


def test_every_live_row_is_rebased_and_paper_rows_are_not():
    book = _host_book()
    changes = repair.plan(book)
    assert len(changes) == 9
    by_id = {row["id"]: ch for row, ch in changes}
    for rid, fill, shares in _LEGACY:
        ch = by_id[rid]
        assert ch["fill_price"] == fill
        assert abs(ch["entry_price"] - 2.0 / shares) < 1e-12
        assert abs(ch["cost_basis"] - 2.06) < 1e-12
        assert ch["above_band"] is True  # 0.987-0.989 against the 0.92 ceiling
    assert all(row["execution"] == "live" for row, _ in changes)


def test_the_three_round_trips_are_recomputed_against_the_true_cost():
    by_id = {row["id"]: ch for row, ch in repair.plan(_host_book())}
    trips = {
        "2026-09-01:prediction:3501950:NO:live": -0.124580,
        "2026-09-01:prediction:2243896:NO:live": -0.163071,
        "2026-08-11:prediction:2774057:NO:live": -0.085884,
    }
    for rid, want in trips.items():
        assert abs(by_id[rid]["realized_return"] - want) < 1e-6, rid


def test_a_row_backfilled_without_proceeds_gets_them_by_exact_inversion():
    by_id = {row["id"]: ch for row, ch in repair.plan(_host_book())}
    ch = by_id["2026-08-11:prediction:2774057:NO:live"]
    assert abs(ch["last_mark"]["proceeds"] - 1.88307871875) < 1e-9
    assert ch["last_mark"]["price"] is None  # never recorded; not invented


def test_legacy_rows_with_no_return_stay_that_way():
    by_id = {row["id"]: ch for row, ch in repair.plan(_host_book())}
    legacy = by_id["2026-08-10:prediction:3324624:NO:live"]
    assert "realized_return" not in legacy
    assert "last_mark" not in legacy


def test_a_row_not_yet_backfilled_is_left_for_backfill():
    """If backfill_settled has not yet run for 2774057 the return is None and the
    repair must not compute one -- backfill will, against the corrected cost."""
    book = _book()
    by_id = {row["id"]: ch for row, ch in repair.plan(book)}
    assert "realized_return" not in by_id["2026-08-11:prediction:2774057:NO:live"]


def test_refuses_a_book_that_is_already_repaired():
    book = _host_book()
    book["positions"][0]["fill_price"] = 0.943
    with pytest.raises(repair.Refused, match="already"):
        repair.plan(book)


def test_refuses_when_the_live_row_count_moved():
    book = _host_book()
    book["positions"].append(_row("2026-09-12:prediction:1:NO:live", 0.9, 2.02))
    with pytest.raises(repair.Refused, match="10"):
        repair.plan(book)


def test_refuses_a_stake_other_than_the_two_dollars_measured():
    book = _host_book()
    book["positions"][3]["cost_basis"] = 5.0
    with pytest.raises(repair.Refused, match="cost_basis"):
        repair.plan(book)


def test_refuses_a_recorded_proceeds_that_disagrees_with_the_venue():
    book = _host_book()
    for p in book["positions"]:
        if p.get("instrument") == "3501950":
            p["last_mark"]["proceeds"] = 1.9
    with pytest.raises(repair.Refused, match="proceeds"):
        repair.plan(book)


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = _write(tmp_path, _host_book())
    before = path.read_text(encoding="utf-8")
    assert repair.main([str(path)]) == 0
    assert path.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "dry run" in out and "9 row" in out


def test_apply_writes_and_keeps_a_backup(tmp_path):
    path = _write(tmp_path, _host_book())
    assert repair.main([str(path), "--apply"]) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    row = next(p for p in after["positions"] if p.get("instrument") == "3501950")
    assert abs(row["cost_basis"] - 2.06) < 1e-12
    assert abs(row["realized_return"] - (-0.124580)) < 1e-6
    assert row["fill_price"] == 0.9224999999999999
    assert list(tmp_path.glob("book.*.bak"))
    # Second run is refused: the rows now carry fill_price.
    assert repair.main([str(path), "--apply"]) == 2
