# tests/test_checkpoint_backfill.py
import trading


def test_snap_close_exact_date_hit():
    closes = {"2026-06-01": 10.0, "2026-06-02": 11.0, "2026-06-03": 12.0}
    assert trading._snap_close(closes, "2026-06-02") == 11.0


def test_snap_close_weekend_snaps_back_to_prior_close():
    # 2026-06-06 is a Saturday with no close; snap to Friday 2026-06-05.
    closes = {"2026-06-04": 10.0, "2026-06-05": 11.0, "2026-06-08": 12.0}
    assert trading._snap_close(closes, "2026-06-06") == 11.0


def test_snap_close_target_before_history_returns_none():
    closes = {"2026-06-10": 10.0}
    assert trading._snap_close(closes, "2026-06-01") is None


def test_snap_close_empty_map_returns_none():
    assert trading._snap_close({}, "2026-06-01") is None
