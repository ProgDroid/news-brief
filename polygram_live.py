"""Real-money write layer for polygram.ink. Fail-closed, None-on-failure.

Reuses trading.py's JWT/token-file auth. Every network helper returns None on
any non-2xx / parse / network error; callers treat None as "did not happen".
"""

import math
from datetime import datetime, timezone

import requests

import common
from common import _load_json_or, log
from trading import POLYGRAM_BASE, POLYGRAM_TOKEN_FILE, polygram_login

_TIMEOUT = 30
_ERROR_BODY_CHARS = 400  # enough for a venue {error, message}, short of a dumped page


def _pg_request(method, path, params=None, json_body=None):
    """Authed request to a polygram.ink path; refresh the JWT once on 401.

    Returns parsed JSON dict on 2xx, else None (network error, non-2xx after a
    refresh attempt, or unparseable body). Mirrors trading._polygram_get.

    A rejection logs the venue's own RESPONSE BODY, and for writes the request body
    that earned it. requests' HTTPError stringifies to only status + URL, so logging
    the exception alone reduced "400 Bad Request" — an error the venue documents as
    {error, message}, i.e. it names the offending field — to an unactionable line.
    That mattered: a fail-closed write layer whose rejections are unattributable
    cannot be debugged from the outside at all. No credentials pass through here
    (login lives in trading.polygram_login), so bodies are safe to log.
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
            detail = (resp.text or "")[:_ERROR_BODY_CHARS].replace("\n", " ")
            sent = f" sent={json_body}" if json_body is not None else ""
            log.warning(
                f"PolyGram {method} {path} failed: {e} "
                f"[status={resp.status_code} body={detail!r}{sent}]"
            )
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
            # DIRECTION of the trade, not which outcome — `outcome` already names
            # that ("Yes"/"No"). Sleeve A is always a buy; it never shorts, it exits
            # via /trade/sell. Required by the venue and returned by no read path, so
            # its absence was invisible until a live order 400'd (2026-08-09).
            "side": "buy",
            "amount": amount,
        },
    )
    order = (data or {}).get("order") if isinstance(data, dict) else None
    if not isinstance(order, dict) or order.get("status") != "filled":
        # Separate "the venue refused the request" from "it accepted it and we didn't
        # recognise the fill". The first is a payload bug (see _pg_request's logged
        # body); the second means real money moved and this row is about to be
        # discarded, so it must never read as the same event.
        if data is None:
            log.warning(
                f"PolyGram place REJECTED for {market_id}/{outcome} — request never "
                f"succeeded; see the preceding response body"
            )
        else:
            log.error(
                f"PolyGram place returned an UNRECOGNISED fill for "
                f"{market_id}/{outcome}: {data} — if this filled, capital is at the "
                f"venue with no book row (run pgdiag)"
            )
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


def sell_position(market_id, outcome, shares):
    """Sell a live position via POST /trade/sell. Returns normalized sale or None.

    THE DOCS AND THE RUNNING API DISAGREE, AND THE API WINS -- on both halves.

    THE REQUEST. The published page (read 2026-09-10) gives `positionId` as the
    only required field with `shares` optional, "omit for full sell". Asked
    exactly that, the venue said:

        sent={'positionId': 'fm_ferreira1996-2774057-No'}
        400 {"error":"marketId, outcome, and a valid positive shares amount
             are required"}

    This venue's 400s enumerate the COMPLETE required set -- that is how the
    missing `side` field on /trade/place was found -- so the body is those three
    and nothing more. `positionId` is deliberately not resent even though the
    docs demand it: the server may well have produced this error BY failing to
    resolve that value, and re-sending it risks reproducing this exact 400.
    Still no `side`. That is a /trade/place requirement and the error does not
    ask for it; the two write endpoints take different payloads.

    THE RESPONSE, MEASURED 2026-09-10 closing 3501950/No -- the first live close
    this code ever drove to completion:

        {'success': True, 'sold': 2.022221, 'price': 0.9165,
         'proceeds': 1.8033655465, 'remainingShares': 0, 'balance': 49.593235}

    FLAT. No `sale` wrapper, and no `status`, no `profit`, no `fee` anywhere.
    This function spent that time reading a nested {"sale": {"status":
    "completed", "sharesSold": ..., "salePrice": ..., "profit": ..., "fee":
    ...}} -- a shape the venue has never sent -- so that completed real-money
    sale parsed as a failure, close_live_position returned False, and the book
    row was never closed (news-brief-sb0). Note /trade/place DOES nest under
    `order`: the two write endpoints genuinely differ, and the symmetry is as
    tempting and as wrong here as it was for `side` and for `position_key`.

    `success` is the only completion signal on offer; there is no status string
    to check. Fields the venue does not send are NOT invented -- `profit` and
    `fee` are absent from the return rather than synthesised, and the one caller
    needs neither: realized_return is computed from proceeds against our own
    cost_basis, and the venue nets its fees out of proceeds before reporting.
    """
    body = {"marketId": market_id, "outcome": outcome, "shares": shares}
    data = _pg_request("POST", "/trade/sell", json_body=body)
    label = f"{market_id}/{outcome}"
    if not isinstance(data, dict) or data.get("success") is not True:
        # Three outcomes, not one. "The request never landed" moved no money.
        # "The venue said success=false" is an explicit refusal, and we take it
        # at its word. "A 2xx we cannot read" is the dangerous one: the venue
        # accepted the order, so capital may ALREADY have moved while this row
        # stays open for a retry that would sell it again. Collapsing the third
        # into the first is exactly what made 2026-09-10's real sale look like a
        # failure, so it is the one that shouts. Mirrors place_market_order.
        if data is None:
            log.warning(
                f"PolyGram sell REJECTED for {label} — request never succeeded; "
                f"see the preceding response body"
            )
        elif isinstance(data, dict) and data.get("success") is False:
            log.warning(f"PolyGram sell REFUSED for {label}: success=false — {data}")
        else:
            log.error(
                f"PolyGram sell returned an UNRECOGNISED payload for {label}: "
                f"{data} — if this sold, capital moved with no book row, and a "
                f"retry would sell again (run pgdiag)"
            )
        return None
    try:
        sale = {
            "shares_sold": float(data["sold"]),
            "sale_price": float(data["price"]),
            "proceeds": float(data["proceeds"]),
            "remaining_shares": float(data.get("remainingShares") or 0.0),
        }
    except (KeyError, TypeError, ValueError) as e:
        # success was True, so this is a sale we cannot read, not a sale that
        # did not happen. Same severity as an unrecognised payload.
        log.error(
            f"PolyGram sale parse failed for {label}: {e} — payload {data}. The "
            f"venue reported success, so capital moved with no book row (run pgdiag)"
        )
        return None
    if sale["remaining_shares"] > 0:
        # A real sale of PART of the holding. Returned, because the money moved
        # and the proceeds must be recorded, but said out loud: the caller is
        # about to stamp a close on a position the venue still holds.
        log.warning(
            f"PolyGram PARTIAL sell for {label}: sold {sale['shares_sold']} of "
            f"{shares} requested, {sale['remaining_shares']} still held at the "
            f"venue — the book is about to close a position that still exists"
        )
    return sale


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


def venue_key(market_id, outcome):
    """Normalized (marketId, outcome) join key for book-row ↔ venue-position matching.

    Normalized because the two sides come from different places and have never been
    compared against real data: the book stores whatever `/search` returned (a str),
    while `/trade/positions` is unverified — no live position has ever existed. A
    plain `==` join fails silently on an int-vs-str id or a "NO"/"No" case
    difference, and the failure is not symmetric: close_live_position merely refuses
    to sell, but reconcile_live_book reads an unmatched row as SETTLED and closes it
    in the book while the capital is still at the venue.
    """
    return (str(market_id), str(outcome).casefold())


def _match_position(venue_positions, market_id, outcome):
    """The venue position a book row refers to, or None if the venue holds none.

    Returns the whole dict rather than an id because "the venue does not hold
    this" and "the venue holds it under a field name we do not read" are
    different facts needing different fixes, and collapsing both into None is
    how a held position came to be reported as absent every hour for a month
    (news-brief-8fy). reconcile_live_book joins on this same key and reached the
    opposite verdict the whole time, which is what made the log line impossible.
    """
    want = venue_key(market_id, outcome)
    for p in venue_positions or []:
        if venue_key(p.get("marketId"), p.get("outcome")) == want:
            return p
    return None


# What /trade/positions calls the thing /trade/sell asks for as `positionId`.
# MEASURED on the live venue 2026-09-10, across all three open positions, after
# a year in which the response shape had never been seen: the payload carries
# avgPrice, chainDrift, chainShares, chainStatus, currentPrice, image, isLost,
# marketId, marketResolved, marketTitle, outcome, position_key, realizedPnl,
# resolvedTitle, shares, tokenId, totalInvested, unrealizedPnl, userId,
# winningOutcome -- and NO `id` and NO `positionId`, which is what this code
# spent that year reading (news-brief-8fy).
#
# position_key is the only field naming the POSITION rather than the market
# (marketId), the outcome token (tokenId) or the account (userId). It is also
# the only snake_case key in an otherwise camelCase response, which reads like a
# server-side composite rather than a venue-native field.
_VENUE_POSITION_ID = "position_key"


def close_live_position(row, reason):
    """Market-sell a live row and stamp realized_return. False (untouched) on failure.

    realized_return is proceeds-relative: proceeds / cost_basis - 1 (net of fees,
    which the venue already deducts from proceeds). Caller holds the book lock.
    """
    venue = list_positions()
    if venue is None:
        log.warning(f"Live close skipped (positions unreadable): {row['id']}")
        return False
    pos = _match_position(venue, row["instrument"], row["outcome"])
    if pos is None:
        log.warning(f"Live close: {row['id']} not on venue; leaving to reconcile")
        return False
    held = pos.get("shares")
    if not isinstance(held, (int, float)) or held <= 0:
        # The venue holds this position but will not say how much of it, and
        # /trade/sell rejects anything but "a valid positive shares amount".
        # Fail closed and name what arrived -- guessing the size of a real-money
        # sell from our own book is how the book becomes the venue's problem.
        log.warning(
            f"Live close: {row['id']} IS held by the venue but its shares are "
            f"{held!r}, so it cannot be sold. Fields present: {sorted(pos)}"
        )
        return False
    # Two records of one holding. A difference is not a rate needing a measured
    # threshold -- it is an exact disagreement, and any real one means our book
    # is wrong about real capital. Reported, never acted on: refusing to sell
    # over a bookkeeping error would strand the position instead.
    booked = row.get("shares")
    if isinstance(booked, (int, float)) and not math.isclose(
        held, booked, rel_tol=1e-9
    ):
        log.warning(
            f"Live close: {row['id']} book and venue DISAGREE on size -- venue "
            f"{held}, book {booked}. Selling the venue's figure; the book is "
            f"the one that is wrong."
        )
    # position_key is logged, not sent: the venue rejects it (see sell_position).
    log.info(
        f"Live close: selling {row['id']} ({pos.get(_VENUE_POSITION_ID)}) shares={held}"
    )
    sale = sell_position(pos["marketId"], pos["outcome"], held)
    if sale is None:
        return False
    cost = row.get("cost_basis") or 0.0
    row["realized_return"] = (sale["proceeds"] / cost - 1.0) if cost else 0.0
    # closed_date is computed FIRST because last_mark reads it. It used to read
    # row.get("closed_date") three lines before the assignment below, so on an
    # open row -- every row this function is ever called on -- it was None. The
    # tell was a repaired row and a natively closed one DISAGREEING on that one
    # field (measured 2026-09-10: 3501950 had the date, 2243896 had null).
    closed = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row["last_mark"] = {
        "date": closed,
        "price": sale["sale_price"],
        "proceeds": sale["proceeds"],
    }
    row["status"] = "closed"
    row["close_reason"] = reason
    row["closed_date"] = closed
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
    live_keys = {venue_key(p.get("marketId"), p.get("outcome")) for p in venue}
    n = 0
    for row in book.get("positions", []):
        if row.get("execution") != "live" or row.get("status") != "open":
            continue
        if venue_key(row["instrument"], row["outcome"]) not in live_keys:
            row["status"] = "closed"
            row["close_reason"] = "settled"
            row["closed_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            n += 1
            log.info(f"LIVE RECONCILE settled {row['id']} (gone from venue)")
    return n


def _parse_trade_history(data):
    """The records list inside a /trade/history payload, or None if unrecognised.

    Split out of trade_history so pgdiag can ask what PRODUCTION makes of a payload
    it is already holding, instead of re-deriving the same logic beside it. A
    control that reimplements its subject moves whenever the subject moves and
    cannot notice -- mirror the predicate, never re-derive it.

    BOTH key names are GUESSES from the docs page, never observed. Keep that in
    mind when reading a None: it means "not under `history` or `trades`", which is
    not the same as "no records".
    """
    if isinstance(data, dict):
        # `orders` is MEASURED (2026-09-10): {"orders": [...], "total": 5}.
        # `history` and `trades` were the docs-derived guesses this code ran on
        # for a year without ever matching; kept only as inert fallbacks, and
        # trade_history() shouts if all three miss.
        items = data.get("orders") or data.get("history") or data.get("trades")
        return items if isinstance(items, list) else None
    return data if isinstance(data, list) else None


def trade_history():
    """Trade execution history via GET /trade/history. The list, or None.

    None is DELIBERATELY two things to the caller -- backfill_settled must no-op on
    either -- but they are not the same event, so the shape failure says so out
    loud. Returning a silent None on a 2xx is how the fallback for news-brief-sb0
    sat broken and unremarked; a fail-closed path needs a status, not a count.
    """
    data = _pg_request("GET", "/trade/history")
    items = _parse_trade_history(data)
    if items is None and data is not None:
        shape = sorted(data) if isinstance(data, dict) else type(data).__name__
        log.error(
            f"PolyGram /trade/history returned 2xx but no records list under "
            f"`history` or `trades`; top level is {shape} — backfill_settled "
            f"cannot fire and both key names are unverified guesses (run pgdiag)"
        )
    return items


def sale_proceeds(record):
    """Dollars returned by one filled sell order, or None if unreadable.

    /trade/history has NO `proceeds` field. backfill_settled reached for one for
    a year and could only ever have produced None. MEASURED 2026-09-10 on the
    sell of 2243896: `amount` is the GROSS value of the trade, equal to
    shares * fillPrice to the last decimal, and the venue nets `totalFee` out of
    it:

        amount 1.7740742174999997 - totalFee 0.05 = 1.7240742174999997

    which is the proceeds figure /trade/sell returned for that same order,
    exactly. So proceeds are DERIVED here, never read.
    """
    try:
        return float(record["amount"]) - float(record.get("totalFee") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None


def backfill_settled(book):
    """Fill realized_return on settled live rows from /trade/history. None history ⇒ no-op.

    realized_return = proceeds / cost_basis - 1, matched by (marketId, outcome)."""
    pending = [
        p
        for p in book.get("positions", [])
        if p.get("execution") == "live"
        and p.get("close_reason") == "settled"
        and p.get("realized_return") is None
    ]
    if not pending:
        return 0
    hist = trade_history()
    if hist is None:
        return 0
    by_key = {}
    for h in hist:
        if not isinstance(h, dict):
            continue
        # BUYS ARE IN HERE TOO. The old join took the first record for a
        # (marketId, outcome) pair regardless of direction, so the opening buy
        # could supply the "proceeds" of the close. `side` and `status` are
        # measured fields; anything else is not a completed sale.
        if h.get("side") != "sell" or h.get("status") != "filled":
            continue
        key = venue_key(h.get("marketId"), h.get("outcome"))
        # Latest wins: a market can be traded more than once, and the close we
        # are backfilling is the most recent sale, not the first.
        prev = by_key.get(key)
        if prev is None or str(h.get("createdAt") or "") >= str(
            prev.get("createdAt") or ""
        ):
            by_key[key] = h
    n = 0
    missing = []
    for p in pending:
        h = by_key.get(venue_key(p.get("instrument"), p.get("outcome")))
        if not h:
            # A row the venue RESOLVED rather than sold has no sell order at
            # all, so this is ordinary, not a fault -- but it is also the whole
            # reason seven rows still carry no return, so say which.
            missing.append(p.get("id"))
            continue
        proceeds = sale_proceeds(h)
        if proceeds is None:
            missing.append(p.get("id"))
            continue
        cost = p.get("cost_basis") or 0.0
        p["realized_return"] = (proceeds / cost - 1.0) if cost else 0.0
        n += 1
    if missing:
        log.info(
            f"Backfill: no filled sell in /trade/history for {len(missing)} settled "
            f"row(s): {missing[:5]} — resolved markets produce no sell order"
        )
    return n
