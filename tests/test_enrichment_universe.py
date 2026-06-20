# tests/test_enrichment_universe.py
import json
from pathlib import Path

from enrichment.universe import (
    Universe,
    build_universe,
    latest_signal_tickers,
    normalize_ticker,
)


def test_normalize_ticker_strips_venue_suffix():
    assert normalize_ticker("SHELl_EQ") == "SHEL"
    assert normalize_ticker("AVAV_US_EQ") == "AVAV"


def test_normalize_ticker_strips_lse_marker():
    assert normalize_ticker("SHELl") == "SHEL"
    assert normalize_ticker("DXJGl") == "DXJG"
    assert normalize_ticker("SIEd") == "SIE"


def test_normalize_ticker_leaves_plain_symbol():
    assert normalize_ticker("AVAV") == "AVAV"
    assert normalize_ticker("CVX") == "CVX"


def test_build_universe_dedups_and_routes_etf_to_theme():
    book = {
        "positions": [
            {"status": "open", "ticker": "CVX", "asset_class": "equity"},
            {"status": "closed", "ticker": "MU", "asset_class": "equity"},
        ]
    }
    watchlist = {
        "items": [
            {"raw": "AVAV", "asset_class": "equity", "instrument": "AVAV_US_EQ"},
            {
                "raw": "SGLN",
                "asset_class": "equity",
                "instrument": "SGLNl_EQ",
            },  # gold ETF
            {
                "raw": "CVX",
                "asset_class": "equity",
                "instrument": "CVX_US_EQ",
            },  # dup of book
        ]
    }
    signal_tickers = ["MU", "RGLD"]
    pins = ["ukraine", "iran"]

    u = build_universe(book, watchlist, signal_tickers, pins)
    assert isinstance(u, Universe)
    # open equity positions + watchlist stocks + signal tickers, ETFs excluded, deduped
    assert u.tickers == ["CVX", "AVAV", "MU", "RGLD"]
    # pins + ETF-derived theme (SGLN -> gold), order: pins first then ETF themes
    assert u.themes == ["ukraine", "iran", "gold"]


def test_latest_signal_tickers_reads_newest_snapshot(tmp_path):
    older = {"signals": [{"ticker": "OLD"}]}
    newer = {"signals": [{"ticker": "CVX"}, {"ticker": None}, {"ticker": "MU"}]}
    (tmp_path / "signals-2026-06-18.json").write_text(json.dumps(older))
    (tmp_path / "signals-2026-06-19.json").write_text(json.dumps(newer))
    assert latest_signal_tickers(Path(tmp_path)) == ["CVX", "MU"]


def test_latest_signal_tickers_empty_when_no_snapshots(tmp_path):
    assert latest_signal_tickers(Path(tmp_path)) == []


def test_latest_signal_tickers_handles_null_and_corrupt(tmp_path):
    # Case 1: signals key is explicitly null
    null_dir = tmp_path / "null_case"
    null_dir.mkdir()
    (null_dir / "signals-2026-06-19.json").write_text('{"signals": null}')
    assert latest_signal_tickers(null_dir) == []

    # Case 2: file contains invalid JSON
    corrupt_dir = tmp_path / "corrupt_case"
    corrupt_dir.mkdir()
    (corrupt_dir / "signals-2026-06-19.json").write_text("not json")
    assert latest_signal_tickers(corrupt_dir) == []
