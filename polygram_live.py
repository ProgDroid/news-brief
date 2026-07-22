"""Real-money write layer for polygram.ink. Fail-closed, None-on-failure.

Reuses trading.py's JWT/token-file auth. Every network helper returns None on
any non-2xx / parse / network error; callers treat None as "did not happen".
"""

from datetime import datetime, timezone

import requests

import common
from common import _load_json_or, log
from trading import POLYGRAM_BASE, POLYGRAM_TOKEN_FILE, polygram_login

_TIMEOUT = 30


def _pg_request(method, path, params=None, json_body=None):
    """Authed request to a polygram.ink path; refresh the JWT once on 401.

    Returns parsed JSON dict on 2xx, else None (network error, non-2xx after a
    refresh attempt, or unparseable body). Mirrors trading._polygram_get.
    """
    token = (_load_json_or(POLYGRAM_TOKEN_FILE, {}) or {}).get(
        "token"
    ) or polygram_login()
    if not token:
        return None
    url = f"{POLYGRAM_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json_body,
                timeout=_TIMEOUT,
            )
        except Exception as e:
            log.warning(f"PolyGram {method} {path} failed: {e}")
            return None
        if resp.status_code == 401 and attempt == 1:
            token = polygram_login()
            if not token:
                return None
            continue
        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"PolyGram {method} {path} failed: {e}")
            return None
    return None


def wallet_balance():
    """Current USD cash balance, or None on failure. GET /wallet."""
    data = _pg_request("GET", "/wallet")
    if not isinstance(data, dict) or "balance" not in data:
        return None
    try:
        return float(data["balance"])
    except (TypeError, ValueError):
        return None


def orderbook(token_id):
    """Live orderbook {bids, asks, spread, midpoint} for a token, or None. GET /orderbook/:id."""
    return _pg_request("GET", f"/orderbook/{token_id}")


def place_market_order(event_id, market_id, token_id, outcome, amount):
    """Market buy via POST /trade/place. Returns a normalized fill or None.

    None when the request fails OR the venue did not report status 'filled'
    (fail-closed: no phantom position row is ever written on a non-fill).
    """
    data = _pg_request(
        "POST",
        "/trade/place",
        json_body={
            "eventId": event_id,
            "marketId": market_id,
            "tokenId": token_id,
            "outcome": outcome,
            "amount": amount,
        },
    )
    order = (data or {}).get("order") if isinstance(data, dict) else None
    if not isinstance(order, dict) or order.get("status") != "filled":
        log.warning(f"PolyGram place not filled for {market_id}/{outcome}: {data}")
        return None
    try:
        return {
            "order_id": order["id"],
            "fill_price": float(order["fillPrice"]),
            "shares": float(order["shares"]),
            "spread_fee": float(order.get("spreadFee") or 0.0),
            "trade_fee": float(order.get("tradeFee") or 0.0),
            "total_fee": float(order.get("totalFee") or 0.0),
            "status": order["status"],
        }
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"PolyGram fill parse failed for {market_id}: {e}")
        return None


def sell_position(position_id, shares=None):
    """Sell a live position via POST /trade/sell. Returns normalized sale or None."""
    body = {"positionId": position_id}
    if shares is not None:
        body["shares"] = shares
    data = _pg_request("POST", "/trade/sell", json_body=body)
    sale = (data or {}).get("sale") if isinstance(data, dict) else None
    if not isinstance(sale, dict) or sale.get("status") != "completed":
        log.warning(f"PolyGram sell not completed for {position_id}: {data}")
        return None
    try:
        return {
            "shares_sold": float(sale["sharesSold"]),
            "sale_price": float(sale["salePrice"]),
            "proceeds": float(sale["proceeds"]),
            "profit": float(sale["profit"]),
            "fee": float(sale.get("fee") or 0.0),
            "status": sale["status"],
        }
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"PolyGram sale parse failed for {position_id}: {e}")
        return None


def list_positions():
    """Open venue positions via GET /trade/positions.

    Returns the list on success (possibly empty), or None if the read FAILED.
    None must never be treated as 'no positions' — see the fail-closed note.
    """
    data = _pg_request("GET", "/trade/positions")
    if not isinstance(data, dict) or "positions" not in data:
        return None
    positions = data["positions"]
    return positions if isinstance(positions, list) else None


def cap_ok(amount, live_exposure):
    """True only if `amount` USD is within every guard. Fail-closed on unreadable cash.

    - per-trade: amount <= PG_LIVE_PER_TRADE_CAP
    - total:     live_exposure + amount <= PG_LIVE_TOTAL_CAP
    - funded:    amount <= wallet_balance() (None balance ⇒ reject)
    """
    if amount <= 0 or amount > common.PG_LIVE_PER_TRADE_CAP:
        return False
    if live_exposure + amount > common.PG_LIVE_TOTAL_CAP:
        return False
    bal = wallet_balance()
    if bal is None or amount > bal:
        return False
    return True


def open_live_position(
    book,
    *,
    sleeve,
    event_id,
    market_id,
    token_id,
    outcome,
    side_index,
    amount,
    topic,
    source_id,
    source_kind,
    source_perspective,
    live_exposure,
):
    """Place a real market buy and append a truthful live row. None if not opened.

    Order of guards (all fail-closed): kill-switch → cap_ok → place. No order is
    placed unless the cap passes; no row is written unless the order fills.
    Caller must hold the book lock.
    """
    if not common.PG_LIVE_ENABLED:
        return None
    if not cap_ok(amount, live_exposure):
        log.warning(f"Live open rejected by cap: {market_id}/{outcome} ${amount}")
        return None
    fill = place_market_order(event_id, market_id, token_id, outcome, amount)
    if fill is None:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {
        "id": f"{today}:prediction:{market_id}:{outcome.upper()}:live",
        "opened": today,
        "asset_class": "prediction",
        "venue": "polygram",
        "execution": "live",
        "sleeve": sleeve,
        "ticker": market_id,
        "instrument": market_id,
        "event_id": event_id,
        "token_id": token_id,
        "outcome": outcome,
        "side_index": side_index,
        "play_type": "resolution",
        "direction": "bullish",  # always long the held side (long-sense return)
        "topic": topic,
        "rationale": f"live open (sleeve {sleeve})",
        "source_id": source_id,
        "source_kind": source_kind,
        "source_perspective": source_perspective,
        "order_id": fill["order_id"],
        "entry_price": fill["fill_price"],
        "shares": fill["shares"],
        "cost_basis": amount,
        "fees": {
            "spread_fee": fill["spread_fee"],
            "trade_fee": fill["trade_fee"],
            "total_fee": fill["total_fee"],
        },
        "entry_date": today,
        "status": "open",
        "close_reason": None,
        "closed_date": None,
        "checkpoints": {},
        "last_mark": None,
        "realized_return": None,
    }
    book["positions"].append(row)
    log.info(
        f"LIVE OPEN {sleeve} {market_id}/{outcome} ${amount} @ {fill['fill_price']}"
    )
    return row


def _match_position_id(venue_positions, market_id, outcome):
    """Find the venue positionId for a book row by (marketId, outcome)."""
    for p in venue_positions or []:
        if p.get("marketId") == market_id and p.get("outcome") == outcome:
            return p.get("id")
    return None


def close_live_position(row, reason):
    """Market-sell a live row and stamp realized_return. False (untouched) on failure.

    realized_return is proceeds-relative: proceeds / cost_basis - 1 (net of fees,
    which the venue already deducts from proceeds). Caller holds the book lock.
    """
    venue = list_positions()
    if venue is None:
        log.warning(f"Live close skipped (positions unreadable): {row['id']}")
        return False
    pos_id = _match_position_id(venue, row["instrument"], row["outcome"])
    if pos_id is None:
        log.warning(f"Live close: {row['id']} not on venue; leaving to reconcile")
        return False
    sale = sell_position(pos_id)
    if sale is None:
        return False
    cost = row.get("cost_basis") or 0.0
    row["realized_return"] = (sale["proceeds"] / cost - 1.0) if cost else 0.0
    row["last_mark"] = {
        "date": row.get("closed_date"),
        "price": sale["sale_price"],
        "proceeds": sale["proceeds"],
    }
    row["status"] = "closed"
    row["close_reason"] = reason
    row["closed_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"LIVE CLOSE {row['id']} reason={reason} proceeds={sale['proceeds']}")
    return True


def reconcile_live_book(book):
    """Make the venue authoritative: settle open live rows the venue no longer holds.

    Returns the count reconciled. If list_positions() FAILED (None), do nothing —
    a failed read must never be read as 'all positions gone'.
    """
    venue = list_positions()
    if venue is None:
        return 0
    live_keys = {(p.get("marketId"), p.get("outcome")) for p in venue}
    n = 0
    for row in book.get("positions", []):
        if row.get("execution") != "live" or row.get("status") != "open":
            continue
        if (row["instrument"], row["outcome"]) not in live_keys:
            row["status"] = "closed"
            row["close_reason"] = "settled"
            row["closed_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            n += 1
            log.info(f"LIVE RECONCILE settled {row['id']} (gone from venue)")
    return n
