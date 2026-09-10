import importlib

import pytest
import requests

import common
import polygram_live


def test_live_config_defaults_off(monkeypatch):
    monkeypatch.delenv("PG_LIVE_ENABLED", raising=False)
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is False
    assert common.PG_LIVE_TOTAL_CAP == 50.0
    assert common.PG_LIVE_PER_TRADE_CAP == 5.0


def test_live_config_comes_from_settings_not_the_environment(monkeypatch):
    """The live-money caps are rows now, not import-time constants.

    Both halves matter. The environment must no longer decide whether real money
    trades — it seeds the rows once and is then inert — and the rows must arrive
    coerced to the right types, because a cap read as a string would compare
    wrongly against an order size rather than fail.
    """
    import config

    monkeypatch.setenv("PG_LIVE_ENABLED", "1")
    importlib.reload(common)
    assert common.PG_LIVE_ENABLED is False, "the environment is not a runtime source"

    monkeypatch.setattr(
        config,
        "_read_settings",
        lambda: {
            "PG_LIVE_ENABLED": "1",
            "PG_LIVE_TOTAL_CAP": "120",
            "PG_LIVE_PER_TRADE_CAP": "3",
        },
    )
    config.invalidate()
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
    # Exact-equality on purpose: the venue rejects the whole order when a required
    # field is absent, and "marketId, outcome, side, and amount are required" is the
    # only feedback it gives. A subset assertion would have let `side` go missing.
    assert captured["body"] == {
        "eventId": "evt_a",
        "marketId": "mkt_b",
        "tokenId": "0xabc",
        "outcome": "Yes",
        "side": "buy",
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
            "sold": 161.29,
            "price": 0.72,
            "proceeds": 116.13,
            "remainingShares": 0,
            "balance": 500.0,
        }

    monkeypatch.setattr(polygram_live, "_pg_request", fake)
    r = polygram_live.sell_position("mkt_1", "No", 1.0)
    assert captured["body"] == {
        "marketId": "mkt_1",
        "outcome": "No",
        "shares": 1.0,
    }
    assert r["proceeds"] == 116.13
    assert r["shares_sold"] == 161.29 and r["sale_price"] == 0.72


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


def _live_row(market_id="mkt_b", outcome="No"):
    return {
        "id": "r1",
        "execution": "live",
        "sleeve": "A",
        "asset_class": "prediction",
        "instrument": market_id,
        "outcome": outcome,
        "side_index": 1,
        "entry_price": 0.80,
        "shares": 6.25,
        "cost_basis": 5.0,
        "status": "open",
        "realized_return": None,
        "closed_date": None,
        "close_reason": None,
    }


def test_match_position_picks_the_row_for_the_right_OUTCOME():
    """Both legs of a market are open at once, so matching the market alone
    would sell the wrong side."""
    venue = [
        {"position_key": "pk_no", "marketId": "mkt_b", "outcome": "No"},
        {"position_key": "pk_yes", "marketId": "mkt_b", "outcome": "Yes"},
    ]
    assert polygram_live._match_position(venue, "mkt_b", "No")["position_key"] == (
        "pk_no"
    )
    assert polygram_live._match_position(venue, "mkt_z", "No") is None


def test_match_position_tolerates_venue_type_and_case_drift():
    # We store market_id as the STRING the search returned ('2774057'). The live
    # response was finally measured on 2026-09-10 and echoes a str -- but the
    # normalisation stays, because that was unverified for a year and the failing
    # direction is silent: reconcile reads an unmatched row as SETTLED.
    venue = [{"position_key": "pk", "marketId": 2774057, "outcome": "NO"}]
    assert polygram_live._match_position(venue, "2774057", "No")["position_key"] == "pk"


def test_reconcile_keeps_a_position_the_venue_still_holds_under_type_drift(monkeypatch):
    # The dangerous direction: a failed join makes reconcile read "not on venue" as
    # SETTLED, closing the book row while the money is still at the venue.
    monkeypatch.setattr(
        polygram_live,
        "list_positions",
        lambda: [{"position_key": "pk", "marketId": 2774057, "outcome": "NO"}],
    )
    row = _live_row()
    row["instrument"] = "2774057"
    row["outcome"] = "No"
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 0
    assert row["status"] == "open"


def _venue_pos(shares=6.25, market_id="mkt_b", outcome="No"):
    """The measured /trade/positions shape (2026-09-10), trimmed to what the
    close path reads."""
    return {
        "position_key": "someone-mkt_b-No",
        "marketId": market_id,
        "outcome": outcome,
        "shares": shares,
        "tokenId": "11134447534296978",
        "userId": "someone",
    }


def _completed_sale(recorder):
    def sell(market_id, outcome, shares):
        recorder.append((market_id, outcome, shares))
        return {
            "proceeds": 6.0,
            "sale_price": 0.96,
            "profit": 1.0,
            "fee": 0.05,
            "shares_sold": 6.25,
            "status": "completed",
        }

    return sell


def test_sell_position_sends_the_fields_the_VENUE_requires(monkeypatch):
    """The docs and the running API disagree, and the API wins.

    Docs (read 2026-09-10): positionId required, shares optional, "omit for
    full sell". The live venue, asked exactly that:

        sent={'positionId': 'fm_ferreira1996-2774057-No'}
        400 {"error":"marketId, outcome, and a valid positive shares amount
             are required"}

    This venue's 400s enumerate the COMPLETE required set -- that is how the
    missing `side` field was found on /trade/place -- so the body is those
    three fields and nothing else. positionId is deliberately NOT resent: the
    server may have produced this very error by failing to resolve it.
    """
    sent = {}
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda m, path, params=None, json_body=None: sent.update(
            {"path": path, "body": json_body}
        )
        or {
            "success": True,
            "sold": 1.0,
            "price": 0.5,
            "proceeds": 0.5,
            "remainingShares": 0,
        },
    )
    polygram_live.sell_position("2774057", "No", 2.02325)
    assert sent["path"] == "/trade/sell"
    assert sent["body"] == {
        "marketId": "2774057",
        "outcome": "No",
        "shares": 2.02325,
    }


def test_close_live_position_sells_the_VENUES_share_count(monkeypatch):
    """The venue is authoritative about what is actually held. The book records
    what we think we bought, and "a valid positive shares amount" is exactly
    what a drifted book figure would fail."""
    sold = []
    monkeypatch.setattr(
        polygram_live, "list_positions", lambda: [_venue_pos(shares=2.02325)]
    )
    monkeypatch.setattr(polygram_live, "sell_position", _completed_sale(sold))
    row = _live_row()
    row["shares"] = 99.0  # a stale book figure

    assert polygram_live.close_live_position(row, "target") is True
    assert sold[0][2] == 2.02325, "the VENUE's share count must be sold"


def test_close_live_position_echoes_the_venues_own_id_and_outcome(monkeypatch):
    """Send back exactly what the venue told us it holds. The book stores the
    market id as the string /search returned and the outcome in its own case;
    echoing the venue's values removes the type/case drift from the write path
    entirely, instead of normalising and hoping."""
    sold = []
    monkeypatch.setattr(
        polygram_live,
        "list_positions",
        lambda: [_venue_pos(market_id="2774057", outcome="No")],
    )
    monkeypatch.setattr(polygram_live, "sell_position", _completed_sale(sold))
    row = _live_row(market_id=2774057, outcome="NO")

    assert polygram_live.close_live_position(row, "target") is True
    assert sold[0][0] == "2774057" and sold[0][1] == "No"


def test_a_book_versus_venue_share_disagreement_is_reported(monkeypatch, caplog):
    """Not a rate, so it needs no measured threshold: this is an exact
    disagreement between two records of the same holding, and any real
    difference is a fact about our book being wrong."""
    sold = []
    monkeypatch.setattr(
        polygram_live, "list_positions", lambda: [_venue_pos(shares=2.02325)]
    )
    monkeypatch.setattr(polygram_live, "sell_position", _completed_sale(sold))
    row = _live_row()
    row["shares"] = 2.5

    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.close_live_position(row, "target") is True
    # On the RECORDS, not on a keyword. Keying this pair to a word the message
    # also chooses means renaming the message silently retires the test -- the
    # mutation run caught exactly that.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "2.02325" in warnings[0].getMessage()
    assert "2.5" in warnings[0].getMessage()
    assert sold[0][2] == 2.02325, (
        "the disagreement is reported, not acted on -- refusing to sell would "
        "strand real capital over a bookkeeping error"
    )


def test_share_counts_that_AGREE_do_not_warn(monkeypatch, caplog):
    """Presence sibling. Without it, a warn-on-everything implementation passes
    the test above, and the signal is worthless."""
    monkeypatch.setattr(
        polygram_live, "list_positions", lambda: [_venue_pos(shares=6.25)]
    )
    monkeypatch.setattr(polygram_live, "sell_position", _completed_sale([]))
    row = _live_row()
    row["shares"] = 6.25

    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.close_live_position(row, "target") is True
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


def test_float_representation_noise_is_not_a_disagreement(monkeypatch, caplog):
    """0.1+0.2 != 0.3 in binary. An exact == would make this alert fire on
    arithmetic rather than on anything about the position."""
    monkeypatch.setattr(
        polygram_live, "list_positions", lambda: [_venue_pos(shares=0.1 + 0.2)]
    )
    monkeypatch.setattr(polygram_live, "sell_position", _completed_sale([]))
    row = _live_row()
    row["shares"] = 0.3

    with caplog.at_level("WARNING", logger="newsbrief"):
        polygram_live.close_live_position(row, "target")
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


def test_close_live_position_false_when_unmatchable(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [])  # not on venue
    row = _live_row()
    assert polygram_live.close_live_position(row, "target") is False
    assert row["status"] == "open"  # untouched


def test_reconcile_settles_missing_positions(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [])  # venue empty
    row = _live_row()
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 1
    assert row["status"] == "closed" and row["close_reason"] == "settled"


def test_reconcile_skips_on_failed_read(monkeypatch):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: None)  # read failed
    row = _live_row()
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 0
    assert row["status"] == "open"  # NEVER mass-settle on a failed read


# --- Two refusals that must not wear the same message (news-brief-8fy).
#
# 'Live close: ...:NO:live not on venue; leaving to reconcile' logged hourly for
# a MONTH against a position opened 2026-08-11. It could not have been true as
# written: reconcile_live_book runs seconds later in the same lock, over the
# same venue read, and settles anything the venue no longer holds -- so a row
# genuinely absent from the venue would have been closed on the first pass.
#
# The two functions match on DIFFERENT fields. reconcile joins on
# (marketId, outcome) and matched; close looked up p['id'] and did not. One
# venue response, two verdicts, and the operator was shown the wrong one.
#
# Every test above hardcodes {"id": "pos_x"}, authored from the same inference
# as the code it covers, so none of them could ever have caught this.


def _held(market_id="mkt_b", outcome="No", **extra):
    """A venue position the book row really matches on (marketId, outcome)."""
    return {"marketId": market_id, "outcome": outcome, **extra}


def test_a_position_the_venue_does_not_hold_is_reported_as_ABSENT(monkeypatch, caplog):
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [])
    row = _live_row()
    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.close_live_position(row, "target") is False
    assert "not on venue" in caplog.text
    assert row["status"] == "open"


def test_a_position_the_venue_HOLDS_is_never_reported_as_absent(monkeypatch, caplog):
    """The discriminating half, and the whole bug. The venue holds this
    position -- reconcile agrees, and declines to settle it -- so calling it
    'not on venue' sends the operator hunting for a position that is right
    there with real capital in it."""
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [_held()])
    row = _live_row()
    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.close_live_position(row, "target") is False
    assert "not on venue" not in caplog.text
    assert row["status"] == "open", "still fail-closed: it must not sell blind"


def test_an_unsellable_position_NAMES_the_fields_it_actually_carried(
    monkeypatch, caplog
):
    """The self-diagnosing part. 424 hourly warnings never once said what the
    venue response actually contained, which is the single fact needed to fix
    it -- the same unactionable shape as an HTTPError that stringifies to a
    status code.

    What makes a position unsellable has moved since: from "carries no id" to
    "carries no usable share count", because `shares` is what /trade/sell
    rejects. The naming requirement is the part that matters, and it survives
    the move.
    """
    monkeypatch.setattr(
        polygram_live,
        "list_positions",
        lambda: [_held(tokenId="tok_x", marketTitle="Will X happen?")],
    )
    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.close_live_position(_live_row(), "target") is False
    assert "tokenId" in caplog.text
    assert "marketTitle" in caplog.text


def test_reconcile_does_NOT_settle_a_position_close_could_not_identify(
    monkeypatch,
):
    """The contradiction, pinned. These two ran seconds apart over the same
    venue read for a month and disagreed; whatever close decides, a position
    the venue still holds must never be booked as settled."""
    monkeypatch.setattr(polygram_live, "list_positions", lambda: [_held()])
    row = _live_row()
    book = {"positions": [row]}
    assert polygram_live.reconcile_live_book(book) == 0
    assert row["status"] == "open" and row["close_reason"] is None


def test_backfill_settled_fills_realized(monkeypatch):
    monkeypatch.setattr(
        polygram_live,
        "trade_history",
        lambda: [
            {"marketId": "m", "outcome": "No", "type": "settlement", "proceeds": 2.5}
        ],
    )
    row = {
        "execution": "live",
        "status": "closed",
        "close_reason": "settled",
        "instrument": "m",
        "outcome": "No",
        "cost_basis": 2.0,
        "realized_return": None,
    }
    book = {"positions": [row]}
    assert polygram_live.backfill_settled(book) == 1
    assert abs(row["realized_return"] - 0.25) < 1e-9  # 2.5/2.0 - 1


def test_backfill_settled_skips_when_history_unreadable(monkeypatch):
    monkeypatch.setattr(polygram_live, "trade_history", lambda: None)
    row = {
        "execution": "live",
        "status": "closed",
        "close_reason": "settled",
        "instrument": "m",
        "outcome": "No",
        "cost_basis": 2.0,
        "realized_return": None,
    }
    book = {"positions": [row]}
    assert polygram_live.backfill_settled(book) == 0
    assert row["realized_return"] is None


# ── A rejection must carry the venue's own explanation ─────────────────────────
# requests' HTTPError stringifies to status + URL only, so logging the exception
# alone turned a documented {error, message} 400 into an unactionable line.


class _Resp:
    def __init__(self, status, body, payload=None):
        self.status_code = status
        self.text = body
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._payload


def test_pg_request_logs_response_and_request_bodies_on_400(monkeypatch, caplog):
    monkeypatch.setattr(
        polygram_live,
        "_load_json_or",
        lambda p, d: {"token": "tok"},
    )
    monkeypatch.setattr(
        polygram_live.requests,
        "request",
        lambda *a, **k: _Resp(
            400, '{"error":"BadRequest","message":"marketId must be an integer"}'
        ),
    )
    with caplog.at_level("WARNING", logger="newsbrief"):
        out = polygram_live._pg_request(
            "POST", "/trade/place", json_body={"marketId": "682705", "amount": 2.0}
        )
    assert out is None
    assert "marketId must be an integer" in caplog.text  # the venue names the field
    assert "status=400" in caplog.text
    assert "sent=" in caplog.text and "682705" in caplog.text  # compare against it


def test_pg_request_truncates_a_huge_error_body(monkeypatch, caplog):
    monkeypatch.setattr(polygram_live, "_load_json_or", lambda p, d: {"token": "tok"})
    monkeypatch.setattr(
        polygram_live.requests,
        "request",
        lambda *a, **k: _Resp(500, "x" * 5000),
    )
    with caplog.at_level("WARNING", logger="newsbrief"):
        polygram_live._pg_request("GET", "/wallet")
    assert len(caplog.text) < 2000  # an HTML error page can't flood the log


def test_place_rejection_and_unrecognised_fill_log_differently(monkeypatch, caplog):
    """One is a payload bug; the other means capital may be at the venue unrecorded."""
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.place_market_order("e", "m", "t", "No", 2.0) is None
    assert "REJECTED" in caplog.text and "capital is at the venue" not in caplog.text

    caplog.clear()
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: {"order": {"id": "o1", "status": "FILLED"}},
    )
    with caplog.at_level("WARNING", logger="newsbrief"):
        assert polygram_live.place_market_order("e", "m", "t", "No", 2.0) is None
    assert "UNRECOGNISED" in caplog.text and "capital is at the venue" in caplog.text


# ── /trade/sell: the shape the venue actually sends ───────────────────────────

# MEASURED on the live venue 2026-09-10, closing 3501950/No -- the first live
# close this code has ever driven to completion. Dated deliberately: a fixture
# for an EXTERNAL system records what was observed on a day, and a suite cannot
# falsify the assumption it was written from (news-brief-8fy).
MEASURED_SELL_2026_09_10 = {
    "success": True,
    "sold": 2.022221,
    "price": 0.9165,
    "proceeds": 1.8033655465,
    "remainingShares": 0,
    "balance": 49.593235,
}


def test_sell_position_parses_the_MEASURED_flat_payload(monkeypatch):
    """The venue answers FLAT. No `sale` wrapper, no status/profit/fee."""
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: dict(MEASURED_SELL_2026_09_10),
    )
    r = polygram_live.sell_position("3501950", "No", 2.022221)
    assert r is not None, "a completed real-money sale must not parse as failure"
    assert r["shares_sold"] == 2.022221
    assert r["sale_price"] == 0.9165
    assert r["proceeds"] == 1.8033655465
    assert r["remaining_shares"] == 0.0


def test_sell_position_unrecognised_payload_says_capital_may_have_moved(
    monkeypatch, caplog
):
    """A 2xx body we cannot read is NOT the same event as a refused request:
    the venue accepted the order, so money may already have moved. It must
    never be logged as the quiet warning a rejection gets."""
    monkeypatch.setattr(
        polygram_live, "_pg_request", lambda *a, **k: {"weird": "shape"}
    )
    with caplog.at_level("WARNING"):
        assert polygram_live.sell_position("3501950", "No", 1.0) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "an unreadable 2xx sell body must log at ERROR"
    assert "capital" in errors[0].getMessage().lower()
    assert "{'weird': 'shape'}" in errors[0].getMessage()


def test_sell_position_request_failure_is_not_reported_as_capital_moved(
    monkeypatch, caplog
):
    """_pg_request already logged the body. None means nothing was sold, so
    this path must stay a warning -- crying ERROR on every network blip is how
    a real capital-moved line stops being read."""
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        assert polygram_live.sell_position("3501950", "No", 1.0) is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("REJECTED" in r.getMessage() for r in caplog.records)


def test_sell_position_reports_a_partial_fill(monkeypatch, caplog):
    """remainingShares > 0 is a real sale of part of the holding. Return it --
    the money moved -- but say so, because the book is about to record a close
    for a position the venue still holds."""
    payload = dict(MEASURED_SELL_2026_09_10, sold=1.0, remainingShares=1.022221)
    monkeypatch.setattr(polygram_live, "_pg_request", lambda *a, **k: payload)
    with caplog.at_level("WARNING"):
        r = polygram_live.sell_position("3501950", "No", 2.022221)
    assert r is not None and r["remaining_shares"] == 1.022221
    assert any("partial" in x.getMessage().lower() for x in caplog.records)


def test_close_live_position_stamps_the_book_from_the_measured_payload(monkeypatch):
    """End to end: the exact venue reply that produced no book row on 2026-09-10
    must now close the row and record what it sold for."""
    monkeypatch.setattr(
        polygram_live,
        "list_positions",
        lambda: [
            {
                "marketId": "3501950",
                "outcome": "No",
                "shares": 2.022221,
                # Shape only; the real key is <userId>-<marketId>-<outcome>.
                "position_key": "acct-3501950-No",
            }
        ],
    )
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: dict(MEASURED_SELL_2026_09_10),
    )
    row = {
        "id": "2026-09-01:prediction:3501950:NO:live",
        "instrument": "3501950",
        "outcome": "No",
        "shares": 2.022221,
        "cost_basis": 2.0,
        "status": "open",
    }
    assert polygram_live.close_live_position(row, "exit") is True
    assert row["status"] == "closed"
    assert row["close_reason"] == "exit"
    assert row["realized_return"] == pytest.approx(1.8033655465 / 2.0 - 1.0)
    assert row["last_mark"]["proceeds"] == 1.8033655465
    assert row["last_mark"]["price"] == 0.9165


def test_sell_position_success_false_is_not_a_sale(monkeypatch, caplog):
    """An explicit refusal is taken at its word: no sale, and no ERROR either.
    The loud line is reserved for a 2xx we cannot READ, where money may have
    moved -- if every non-sale shouted, that distinction would be worthless."""
    monkeypatch.setattr(
        polygram_live,
        "_pg_request",
        lambda *a, **k: dict(MEASURED_SELL_2026_09_10, success=False),
    )
    with caplog.at_level("WARNING"):
        assert polygram_live.sell_position("3501950", "No", 1.0) is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("REFUSED" in r.getMessage() for r in caplog.records)


def test_close_stamps_last_mark_with_TODAYS_date_not_None(monkeypatch):
    """last_mark read row["closed_date"] three lines before it was assigned, so
    on an open row -- the only kind this is called on -- it was always None. The
    tell was live: a row repaired by script carried the date and a natively
    closed one carried null, for the same day."""
    monkeypatch.setattr(
        polygram_live,
        "list_positions",
        lambda: [{"marketId": "1", "outcome": "No", "shares": 2.0}],
    )
    monkeypatch.setattr(
        polygram_live, "_pg_request", lambda *a, **k: dict(MEASURED_SELL_2026_09_10)
    )
    row = {"id": "r", "instrument": "1", "outcome": "No", "cost_basis": 2.0}
    assert polygram_live.close_live_position(row, "manual") is True
    assert row["last_mark"]["date"] is not None
    assert row["last_mark"]["date"] == row["closed_date"], (
        "the mark and the close must agree on when it happened"
    )
