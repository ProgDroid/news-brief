"""One-off repair of the 2026-06 unit-bug rows (docs/2026-08-16-trading-retrospective.md).

The repair is targeted by position id rather than by a detector: this bug class died
with the Stooq->Yahoo cutover, so a general detector could only ever mis-fire on a
row nobody has reasoned about.
"""

import pytest

from scripts.repair_unit_bug_rows import repair_book


def _rr_row():
    """The real 2026-06-02 Rolls-Royce row: entered in pence, marked in pounds."""
    return {
        "id": "2026-06-02:RRl_EQ:bullish",
        "ticker": "RRl_EQ",
        "asset_class": "equity",
        "direction": "bullish",
        "instrument": "rr.uk",
        "entry_price": 1264.0,
        "status": "closed",
        "close_reason": "horizon",
        "play_type": None,
        "checkpoints": {
            "1w": {
                "date": "2026-06-15",
                "price": 13.67199951171875,
                "return": -0.989183544690096,
            },
            "2w": {
                "date": "2026-06-19",
                "price": 14.0293505859375,
                "return": -0.9889008302326444,
            },
            "4w": {
                "date": "2026-06-30",
                "price": 14.445999755859376,
                "return": -0.9885712027247948,
                "price_basis": "historical",
            },
        },
        "last_mark": {
            "date": "2026-07-02",
            "price": 14.75199951171875,
            "return": -0.9883291143103491,
        },
        "realized_return": -0.9885712027247948,
        "haircut": 0.001,
        "net_return": -0.9895712027247948,
        "benchmark_return": None,
        "edge": None,
    }


def _exv1_row():
    """The real 2026-06-26 EXV1 row: benchmark level captured 10x low."""
    return {
        "id": "2026-06-26:equity:EXV1:bullish",
        "ticker": "EXV1",
        "asset_class": "equity",
        "direction": "bullish",
        "status": "closed",
        "benchmark_entry": 733.33,
        "benchmark_return": 9.028254700518525,
        "edge": -9.039030729962834,
        "net_return": -0.010776029444308888,
    }


def test_rescales_entry_price_by_100():
    book = {"positions": [_rr_row()]}
    repair_book(book)
    assert book["positions"][0]["entry_price"] == pytest.approx(12.64)


def test_recovers_the_real_return_from_the_stored_prices():
    """Booked -98.86%; the trade actually made ~+14.3% to its 4w close."""
    book = {"positions": [_rr_row()]}
    repair_book(book)
    p = book["positions"][0]
    # 4w close 14.446 against a true entry of 12.64
    assert p["checkpoints"]["4w"]["return"] == pytest.approx(
        (14.445999755859376 - 12.64) / 12.64
    )
    assert p["realized_return"] == pytest.approx(p["checkpoints"]["4w"]["return"])
    assert p["net_return"] == pytest.approx(p["realized_return"] - 0.001)


def test_recomputes_every_checkpoint_and_the_last_mark():
    book = {"positions": [_rr_row()]}
    repair_book(book)
    p = book["positions"][0]
    for h in ("1w", "2w", "4w"):
        cp = p["checkpoints"][h]
        assert cp["return"] == pytest.approx((cp["price"] - 12.64) / 12.64)
    assert p["last_mark"]["return"] == pytest.approx(
        (14.75199951171875 - 12.64) / 12.64
    )


def test_realized_return_follows_the_source_the_close_actually_used():
    """A reversal close marks at last_mark, not at a horizon checkpoint."""
    row = _rr_row()
    row["close_reason"] = "reversal"
    row["realized_return"] = row["last_mark"]["return"]
    book = {"positions": [row]}
    repair_book(book)
    p = book["positions"][0]
    assert p["realized_return"] == pytest.approx(p["last_mark"]["return"])


def test_bearish_row_return_keeps_its_sign_convention():
    row = _rr_row()
    row["direction"] = "bearish"
    row["realized_return"] = row["checkpoints"]["4w"]["return"]
    book = {"positions": [row]}
    repair_book(book)
    cp = book["positions"][0]["checkpoints"]["4w"]
    assert cp["return"] == pytest.approx(-(14.445999755859376 - 12.64) / 12.64)


def test_rescales_the_corrupt_benchmark_level_by_10():
    book = {"positions": [_exv1_row()]}
    repair_book(book)
    p = book["positions"][0]
    assert p["benchmark_entry"] == pytest.approx(7333.3)
    # the close-time index level is recoverable: entry * (1 + stored return)
    level = 733.33 * (1 + 9.028254700518525)
    assert p["benchmark_return"] == pytest.approx((level - 7333.3) / 7333.3)
    assert p["edge"] == pytest.approx(p["net_return"] - p["benchmark_return"])


def test_is_idempotent():
    book = {"positions": [_rr_row(), _exv1_row()]}
    repair_book(book)
    once = [dict(p) for p in book["positions"]]
    repair_book(book)
    assert book["positions"] == once


def test_leaves_untargeted_rows_alone():
    other = {"id": "2026-07-01:equity:AAPL:bullish", "entry_price": 100.0}
    book = {"positions": [other]}
    assert repair_book(book) == []
    assert book["positions"][0]["entry_price"] == 100.0


def test_refuses_a_row_that_does_not_match_what_was_analysed():
    """The host book has moved on since the snapshot; fail loudly, never guess."""
    row = _rr_row()
    row["entry_price"] = 12.64  # someone already fixed it by hand
    row["realized_return"] = 0.143
    book = {"positions": [row]}
    with pytest.raises(ValueError, match="unexpected state"):
        repair_book(book, strict=True)
