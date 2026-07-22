import importlib

import common
import polygram_live


def test_live_config_defaults_off(monkeypatch):
    monkeypatch.delenv("PG_LIVE_ENABLED", raising=False)
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is False
    assert common.PG_LIVE_TOTAL_CAP == 50.0
    assert common.PG_LIVE_PER_TRADE_CAP == 5.0


def test_live_config_reads_env(monkeypatch):
    monkeypatch.setenv("PG_LIVE_ENABLED", "1")
    monkeypatch.setenv("PG_LIVE_TOTAL_CAP", "120")
    monkeypatch.setenv("PG_LIVE_PER_TRADE_CAP", "3")
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is True
    assert common.PG_LIVE_TOTAL_CAP == 120.0
    assert common.PG_LIVE_PER_TRADE_CAP == 3.0
    monkeypatch.delenv("PG_LIVE_ENABLED", raising=False)
    importlib.reload(common)


def test_wallet_balance_parses(monkeypatch):
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: {"balance": 1250.0, "currency": "USD"},
    )
    assert polygram_live.wallet_balance() == 1250.0


def test_wallet_balance_none_on_failure(monkeypatch):
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    assert polygram_live.wallet_balance() is None


def test_orderbook_spread_passthrough(monkeypatch):
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: {"bids": [], "asks": [], "spread": 0.02, "midpoint": 0.62},
    )
    ob = polygram_live.orderbook("0xabc")
    assert ob["spread"] == 0.02 and ob["midpoint"] == 0.62


def test_place_market_order_normalizes_fill(monkeypatch):
    captured = {}

    def fake(method, path, params=None, json_body=None):
        captured["path"] = path
        captured["body"] = json_body
        return {
            "success": True,
            "order": {
                "id": "ord_1",
                "fillPrice": 0.62,
                "shares": 161.29,
                "spreadFee": 1.5,
                "tradeFee": 0.5,
                "totalFee": 2.0,
                "status": "filled",
            },
        }

    monkeypatch.setattr(polygram_live, "_pg_request", fake)
    fill = polygram_live.place_market_order("evt_a", "mkt_b", "0xabc", "Yes", 100)
    assert captured["path"] == "/trade/place"
    assert captured["body"] == {
        "eventId": "evt_a",
        "marketId": "mkt_b",
        "tokenId": "0xabc",
        "outcome": "Yes",
        "amount": 100,
    }
    assert fill == {
        "order_id": "ord_1",
        "fill_price": 0.62,
        "shares": 161.29,
        "spread_fee": 1.5,
        "trade_fee": 0.5,
        "total_fee": 2.0,
        "status": "filled",
    }


def test_place_market_order_none_when_unfilled(monkeypatch):
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: {"success": True, "order": {"status": "rejected"}},
    )
    assert polygram_live.place_market_order("e", "m", "t", "Yes", 5) is None


def test_sell_position_full(monkeypatch):
    captured = {}

    def fake(method, path, params=None, json_body=None):
        captured["body"] = json_body
        return {
            "success": True,
            "sale": {
                "sharesSold": 161.29,
                "salePrice": 0.72,
                "proceeds": 116.13,
                "profit": 14.13,
                "fee": 1.16,
                "status": "completed",
            },
        }

    monkeypatch.setattr(polygram_live, "_pg_request", fake)
    r = polygram_live.sell_position("pos_1")
    assert captured["body"] == {"positionId": "pos_1"}
    assert r["proceeds"] == 116.13 and r["status"] == "completed"


def test_list_positions_empty_on_failure(monkeypatch):
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    assert polygram_live.list_positions() is None  # None = couldn't read (see note)


def test_cap_ok_rejects_over_per_trade(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(6.0, live_exposure=0.0) is False


def test_cap_ok_rejects_over_total(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(5.0, live_exposure=48.0) is False


def test_cap_ok_rejects_when_balance_unreadable(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: None)
    assert polygram_live.cap_ok(5.0, live_exposure=0.0) is False


def test_cap_ok_allows_within_all_limits(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_PER_TRADE_CAP", 5.0)
    monkeypatch.setattr(common, "PG_LIVE_TOTAL_CAP", 50.0)
    monkeypatch.setattr(polygram_live, "wallet_balance", lambda: 100.0)
    assert polygram_live.cap_ok(5.0, live_exposure=10.0) is True


def _fill():
    return {
        "order_id": "ord_1",
        "fill_price": 0.62,
        "shares": 8.06,
        "spread_fee": 0.07,
        "trade_fee": 0.03,
        "total_fee": 0.10,
        "status": "filled",
    }


def test_open_live_position_writes_truthful_row(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(polygram_live, "cap_ok", lambda *a, **k: True)
    monkeypatch.setattr(polygram_live, "place_market_order", lambda *a, **k: _fill())
    book = {"positions": []}
    row = polygram_live.open_live_position(
        book,
        sleeve="A",
        event_id="evt_a",
        market_id="mkt_b",
        token_id="0xabc",
        outcome="No",
        side_index=1,
        amount=5.0,
        topic="Hormuz normal by Aug 31?",
        source_id="OilPrice.com",
        source_kind="wire",
        source_perspective=None,
        live_exposure=0.0,
    )
    assert row is not None
    assert row["execution"] == "live" and row["sleeve"] == "A"
    assert row["asset_class"] == "prediction" and row["venue"] == "polygram"
    assert row["instrument"] == "mkt_b" and row["event_id"] == "evt_a"
    assert row["outcome"] == "No" and row["side_index"] == 1
    assert row["entry_price"] == 0.62 and row["shares"] == 8.06
    assert row["cost_basis"] == 5.0 and row["fees"]["total_fee"] == 0.10
    assert row["status"] == "open" and row["source_kind"] == "wire"
    assert book["positions"][-1] is row


def test_open_live_position_noop_when_killswitch_off(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", False)
    book = {"positions": []}
    assert (
        polygram_live.open_live_position(
            book,
            sleeve="A",
            event_id="e",
            market_id="m",
            token_id="t",
            outcome="Yes",
            side_index=0,
            amount=5.0,
            topic="x",
            source_id=None,
            source_kind="unknown",
            source_perspective=None,
            live_exposure=0.0,
        )
        is None
    )
    assert book["positions"] == []


def test_open_live_position_noop_on_cap_fail(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(polygram_live, "cap_ok", lambda *a, **k: False)
    placed = []
    monkeypatch.setattr(
        polygram_live, "place_market_order", lambda *a, **k: placed.append(1)
    )
    book = {"positions": []}
    assert (
        polygram_live.open_live_position(
            book,
            sleeve="A",
            event_id="e",
            market_id="m",
            token_id="t",
            outcome="Yes",
            side_index=0,
            amount=99.0,
            topic="x",
            source_id=None,
            source_kind="unknown",
            source_perspective=None,
            live_exposure=0.0,
        )
        is None
    )
    assert placed == []  # cap checked BEFORE any order is placed
    assert book["positions"] == []
