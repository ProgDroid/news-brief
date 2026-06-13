#!/usr/bin/env python3
"""Equity paper-trading layer: Stooq ticker resolution + pricing, the paper
book, return math, position close, and the open/mark-to-market/scorecard
functions. Imports infra from common.py. (Later phases generalise this to a
multi-asset subsystem.)"""

import json
import requests
from datetime import datetime, timezone, timedelta

from common import (
    DATA_DIR,
    SIGNALS_DIR,
    log,
    _write_json_atomic,
    _load_json_or,
    T212_API_KEY_ID,
    T212_API_KEY,
    T212_BASE_URL,
    t212_auth_header,
    MODEL,  # noqa: F401 — re-exported for later prediction-seam tasks
    ANTHROPIC_HEADERS,  # noqa: F401 — re-exported for later prediction-seam tasks
    POLYGRAM_EMAIL,
    POLYGRAM_PASSWORD,
)

PAPER_DIR = DATA_DIR / "paper"
BOOK_FILE = PAPER_DIR / "book.json"
# Legacy equity-only book; read once and migrated into BOOK_FILE on first load.
LEGACY_PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"
CRYPTO_TICKER_MAP_FILE = PAPER_DIR / "crypto_ticker_map.json"

# Asset-class → informational venue tag stamped on opened positions.
_VENUE_BY_ASSET = {"equity": "t212", "crypto": "kraken", "prediction": "polygram"}

# Crypto majors → Kraken base asset (encodes Kraken's BTC→XBT, DOGE→XDG quirks).
# Signals carry plain symbols (BTC, ETH); the realistic tradable universe is small
# and stable, so a static map + an optional crypto_ticker_map.json override beats a
# catalogue fetch. Unlisted/exotic coins resolve to None (skipped + logged).
_KRAKEN_QUOTE = "USD"
_KRAKEN_BASE = {
    "BTC": "XBT",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "ADA": "ADA",
    "DOT": "DOT",
    "LINK": "LINK",
    "LTC": "LTC",
    "DOGE": "XDG",
    "AVAX": "AVAX",
}

PAPER_HORIZONS = {"1w": 7, "2w": 14, "4w": 28}  # days from entry_date
PAPER_CLOSE_HORIZON = "4w"  # close the position once this checkpoint is recorded

# ── PolyGram (prediction markets) ─────────────────────────────────────────────
POLYGRAM_BASE = "https://polygram.ink/api"
POLYGRAM_TOKEN_FILE = PAPER_DIR / "polygram_token.json"
PG_CANDIDATE_CAP = 25  # max candidate markets fed to the matcher (prompt-size bound)
PG_SIMILARITY_FLOOR = 0.60  # open only matches at/above this matcher similarity
PG_MAX_HOLD_DAYS = 182  # ~26w backstop close for never-resolving resolution markets

# Map a T212 instrument currency (and ISIN country for EUR) to a Stooq market suffix.
_STOOQ_SUFFIX = {"USD": "us", "GBP": "uk", "GBX": "uk"}
_STOOQ_EUR_BY_ISIN = {"DE": "de", "FR": "fr"}

# T212's two-part non-US tickers append a lowercase exchange-marker letter to the
# symbol (Rolls-Royce on the LSE is 'RRl_EQ', the iShares Banks ETF on Xetra is
# 'EXV1d_EQ'). Stooq wants the bare symbol ('rr.uk', 'exv1.de'), so the marker is
# stripped for the resolved market. Keyed by Stooq suffix; add markets as observed.
_STOOQ_MARKET_MARKER = {"uk": "l", "de": "d"}

# When a plain signal symbol matches several T212 listings (same base, different
# exchanges), prefer the US listing — signals carry US-style symbols (SHEL, EQNR,
# TSM) — then UK, then EUR markets. Lower rank wins.
_COUNTRY_PREFERENCE = {"US": 0, "GB": 1, "UK": 1, "DE": 2, "FR": 3}


def fetch_stooq_price(stooq_symbol: str) -> float | None:
    """Fetch the latest close price for a Stooq symbol (e.g. 'aapl.us').

    Returns None on network error or Stooq's 'N/D' not-found sentinel — callers MUST treat
    None as 'could not price' and skip, never substitute a guessed value.
    """
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq fetch failed for {stooq_symbol}: {e}")
        return None
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")  # Symbol,Date,Time,Open,High,Low,Close,Volume
    if len(cols) < 7 or cols[6] in ("N/D", ""):
        log.warning(f"Stooq returned no price for {stooq_symbol}")
        return None
    try:
        price = float(cols[6])
    except ValueError:
        return None
    if price <= 0:
        # Stooq emits 0 for some halted/delisted lines; a 0 entry_price would
        # later divide-by-zero in _signal_return and kill the weekly MtM run.
        log.warning(f"Stooq returned non-positive price for {stooq_symbol}: {price}")
        return None
    return price


def fetch_kraken_price(pair: str) -> float | None:
    """Fetch the last-trade price for a Kraken pair (e.g. 'XBTUSD') via the public API.

    Returns None on network error, a non-empty Kraken error array, an empty/garbled
    result, or a non-positive price — callers MUST treat None as 'could not price' and
    skip, never substitute a guessed value (mirrors fetch_stooq_price).
    """
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        log.warning(f"Kraken error for {pair}: {data['error']}")
        return None
    result = data.get("result") or {}
    if not result:
        log.warning(f"Kraken returned no result for {pair}")
        return None
    # Kraken keys the result by its canonical pair name (e.g. XXBTZUSD), not the
    # queried alias — take the single entry rather than indexing by `pair`.
    entry = next(iter(result.values()))
    try:
        price = float(entry["c"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        log.warning(f"Kraken price unparseable for {pair}")
        return None
    if price <= 0:
        log.warning(f"Kraken returned non-positive price for {pair}: {price}")
        return None
    return price


def _parse_pg_market(m: dict) -> dict | None:
    """Flatten a raw PolyGram market into the fields the seam needs.

    PolyGram mirrors Polymarket: `outcomes`, `outcomePrices`, and `clobTokenIds`
    are JSON-ENCODED STRINGS of index-aligned arrays (YES=index 0, NO=index 1).
    Returns None if the required arrays are missing or unparseable — callers skip.
    """
    try:
        prices = [float(x) for x in json.loads(m["outcomePrices"])]
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (KeyError, TypeError, ValueError):
        return None
    if len(prices) < 2:
        return None
    return {
        "market_id": str(m.get("id", "")),
        "question": str(m.get("question", "")),
        "prices": prices,
        "yes_price": prices[0],
        "end_date": m.get("endDate"),
        "closed": bool(m.get("closed")),
        "uma_status": m.get("umaResolutionStatus"),
        "token_ids": token_ids,
    }


def polygram_login() -> str | None:
    """Log in with POLYGRAM_EMAIL/PASSWORD, persist and return the JWT (or None)."""
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        return None
    try:
        resp = requests.post(
            f"{POLYGRAM_BASE}/auth/login",
            json={"email": POLYGRAM_EMAIL, "password": POLYGRAM_PASSWORD},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
    except Exception as e:
        log.warning(f"PolyGram login failed: {e}")
        return None
    if not token:
        log.warning("PolyGram login returned no token")
        return None
    _write_json_atomic(POLYGRAM_TOKEN_FILE, {"token": token})
    return token


def _polygram_get(path: str, params: dict | None = None):
    """GET a PolyGram path with the persisted JWT; refresh once on 401.

    Returns parsed JSON or None on any failure (uncredentialed, network error,
    non-2xx after a refresh attempt) — same None-on-failure posture as the pricers.
    """
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        return None
    token = (_load_json_or(POLYGRAM_TOKEN_FILE, {}) or {}).get(
        "token"
    ) or polygram_login()
    if not token:
        return None
    url = f"{POLYGRAM_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            )
        except Exception as e:
            log.warning(f"PolyGram GET {path} failed: {e}")
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
            log.warning(f"PolyGram GET {path} failed: {e}")
            return None
    return None


def polygram_search(query: str) -> list | None:
    """Search PolyGram events/markets by free text. Returns the raw events list or None."""
    return _polygram_get("/search", params={"q": query})


def polygram_market(market_id: str) -> dict | None:
    """Fetch one market's full detail (mark + settlement status). Returns raw dict or None."""
    return _polygram_get(f"/markets/{market_id}")


def fetch_price(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument to market via the pricer for its asset class.

    Equity → Stooq, crypto → Kraken. Returns None on any pricing failure —
    callers skip, never guess.
    """
    if asset_class == "crypto":
        return fetch_kraken_price(instrument)
    return fetch_stooq_price(instrument)


def price_position(p: dict) -> float | None:
    """Mark a position to market by dispatching on its asset_class."""
    return fetch_price(p.get("asset_class", "equity"), p["instrument"])


def load_ticker_overrides() -> dict:
    """Manual T212-ticker -> Stooq-symbol overrides for instruments that don't map automatically."""
    return _load_json_or(TICKER_MAP_FILE, {})


def load_instruments_cache() -> dict:
    return _load_json_or(INSTRUMENTS_CACHE_FILE, {})


def refresh_instruments_cache(max_age_days: int = 14, force: bool = False) -> dict:
    """Refresh the T212 instrument metadata cache (ticker -> isin/currencyCode) if stale.

    One rate-limited call (1 req / 50s) returns the full catalogue. Returns the cache dict;
    returns the existing/empty cache unchanged when T212_API_KEY is unset or the call fails.
    """
    cache = load_instruments_cache()
    if not force and cache.get("fetched_at"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                cache["fetched_at"]
            )
            if age < timedelta(days=max_age_days):
                return cache
        except ValueError:
            pass
    if not T212_API_KEY and not T212_API_KEY_ID:
        return cache

    auth_header = t212_auth_header()

    try:
        resp = requests.get(
            f"{T212_BASE_URL}/api/v0/equity/metadata/instruments",
            headers={"Authorization": auth_header},
            timeout=30,
        )
        resp.raise_for_status()
        instruments = {
            i["ticker"]: {
                "isin": i.get("isin", ""),
                "currencyCode": i.get("currencyCode", ""),
            }
            for i in resp.json()
            if i.get("ticker")
        }
    except Exception as e:
        log.warning(f"Instrument cache refresh failed: {e}")
        return cache
    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "instruments": instruments,
    }
    # indent=None: the full T212 catalogue is ~10k instruments; keep it compact.
    _write_json_atomic(INSTRUMENTS_CACHE_FILE, cache, indent=None)
    log.info(f"Instrument cache refreshed: {len(instruments)} instruments")
    return cache


def _match_instrument_by_base(symbol: str, instruments: dict) -> dict | None:
    """Find the cached T212 instrument whose base symbol matches `symbol`.

    Signals carry plain exchange symbols ('SHEL'), while T212 tickers are
    '<SYMBOL>_<COUNTRY>_EQ' (US) or '<SYMBOLl>_EQ' (LSE). The base is the part
    before the first '_'. When several listings share a base, prefer the US one
    (signals use US-style symbols). Returns the metadata dict or None.
    """
    want = symbol.split("_")[0].upper()
    candidates = []
    for tkr, meta in instruments.items():
        parts = tkr.split("_")
        if parts[0].upper() != want:
            continue
        country = parts[1].upper() if len(parts) > 2 else ""
        candidates.append((_COUNTRY_PREFERENCE.get(country, 9), tkr, meta))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def resolve_stooq_symbol(ticker: str, cache: dict, overrides: dict) -> str | None:
    """Map a signal ticker to a Stooq symbol (e.g. 'aapl.us').

    Resolution order:
      1. Manual override file (authoritative).
      2. Exact T212 ticker match in the instrument cache (e.g. 'AAPL_US_EQ').
      3. Base-symbol match: signals usually carry the plain exchange symbol
         ('SHEL', 'BP'), so match it against each instrument's base (the segment
         before the first '_'), preferring the US listing on ambiguity.
    The suffix is derived from the matched instrument's real currency (and ISIN
    country for EUR). Returns None when nothing resolves — callers skip and log.
    """
    if ticker in overrides:
        return overrides[ticker]
    instruments = cache.get("instruments", {})
    meta = instruments.get(ticker)
    if meta is None:
        meta = _match_instrument_by_base(ticker, instruments)
    if not meta:
        return None
    parts = ticker.split("_")
    base = parts[0].lower()
    ccy = (meta.get("currencyCode") or "").upper()
    suffix = _STOOQ_SUFFIX.get(ccy)
    if suffix is None and ccy == "EUR":
        suffix = _STOOQ_EUR_BY_ISIN.get((meta.get("isin") or "")[:2].upper())
    if suffix is None:
        return None
    # Only the two-part LSE/Xetra form ('RRl_EQ', 'EXV1d_EQ') carries the trailing
    # market-marker letter; the three-part US/country form ('AAPL_US_EQ') is clean,
    # and a plain signal symbol ('SHEL') has no underscore — neither is stripped.
    marker = _STOOQ_MARKET_MARKER.get(suffix)
    if len(parts) == 2 and marker and base.endswith(marker):
        base = base[:-1]
    return f"{base}.{suffix}"


def load_crypto_ticker_overrides() -> dict:
    """Manual signal-ticker -> Kraken-pair overrides (mirrors load_ticker_overrides)."""
    return _load_json_or(CRYPTO_TICKER_MAP_FILE, {})


def resolve_kraken_pair(ticker: str, overrides: dict) -> str | None:
    """Map a signal crypto ticker (BTC, ETH, ...) to a Kraken USD pair (e.g. 'XBTUSD').

    Override file is authoritative; otherwise the static majors map supplies the base
    asset and the USD quote is appended. Returns None for unmapped tickers — callers
    skip and log (same posture as an unresolvable equity).
    """
    if ticker in overrides:
        return overrides[ticker]
    base = _KRAKEN_BASE.get(ticker.strip().upper())
    if base is None:
        return None
    return f"{base}{_KRAKEN_QUOTE}"


def _migrate_position(p: dict) -> dict:
    """Stamp a legacy equity position into the polymorphic shape (idempotent)."""
    p.setdefault("asset_class", "equity")
    p.setdefault("venue", "t212")
    p.setdefault("execution", "paper")
    p.setdefault("play_type", None)
    if "instrument" not in p:
        p["instrument"] = p.pop("stooq_symbol", None)
    return p


def load_book() -> dict:
    """Load the unified position book, migrating the legacy equity book once.

    If book.json exists it is authoritative. Otherwise, if the legacy
    paper-book.json exists, its positions are stamped into the polymorphic
    shape (asset_class/venue/execution/instrument/play_type) and written to
    book.json; the legacy file is left in place as a backup. A missing pair
    yields the empty-book shape.
    """
    if BOOK_FILE.exists():
        return _load_json_or(BOOK_FILE, {"positions": []})
    legacy = _load_json_or(LEGACY_PAPER_BOOK_FILE, None)
    if legacy is None:
        return {"positions": []}
    legacy["positions"] = [_migrate_position(p) for p in legacy.get("positions", [])]
    _write_json_atomic(BOOK_FILE, legacy)
    log.info(f"Migrated {len(legacy['positions'])} position(s) to book.json")
    return legacy


def save_book(book: dict):
    _write_json_atomic(BOOK_FILE, book)


def _signal_return(direction: str, entry: float, price: float) -> float:
    """Directional return ratio: +1 for bullish, -1 for bearish, FX/unit-neutral."""
    sign = 1.0 if direction == "bullish" else -1.0
    return sign * (price / entry - 1.0)


def _close_position_at_market(p: dict, day: str, reason: str) -> bool:
    """Close one open position at the current market mark, stamping realized_return.

    Shared by the weekly horizon close, the /close command, and reversal closes.
    Returns False (leaving the position open) when pricing fails.
    """
    price = price_position(p)
    if price is None:
        return False
    ret = _signal_return(p["direction"], p["entry_price"], price)
    p["last_mark"] = {"date": day, "price": price, "return": ret}
    p["realized_return"] = ret
    p["status"] = "closed"
    p["close_reason"] = reason
    p["closed_date"] = day
    return True


def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Each medium/high-confidence directional signal with a resolvable instrument opens one
    notional paper position (deduped per asset_class+ticker+direction). Equity prices come
    from Stooq; unmappable tickers, pricing failures, and macro/null-ticker signals are
    skipped and logged. Marking-to-market and closing happen in the weekly job.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return

    signals = json.loads(snap_path.read_text()).get("signals", [])
    actionable = [
        s
        for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("confidence") in ("medium", "high")
        and s.get("ticker")
    ]
    if not actionable:
        log.info("No actionable signals today")
        return

    book = load_book()

    def _open_keys() -> set:
        return {
            (p.get("asset_class", "equity"), p["ticker"], p["direction"])
            for p in book["positions"]
            if p["status"] == "open"
        }

    open_keys = _open_keys()
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()
    crypto_overrides = load_crypto_ticker_overrides()

    opened = 0
    for s in actionable:
        ac = s.get("asset_class", "equity")
        ticker, direction = s["ticker"], s["direction"]
        opposite = "bearish" if direction == "bullish" else "bullish"

        # Reversal: a fresh opposite-direction call closes the standing position first.
        if (ac, ticker, opposite) in open_keys:
            for p in book["positions"]:
                if (
                    p["status"] == "open"
                    and p.get("asset_class", "equity") == ac
                    and p["ticker"] == ticker
                    and p["direction"] == opposite
                    and _close_position_at_market(p, today, "reversal")
                ):
                    log.info(f"Paper reversal: closed {ac} {ticker} {opposite}")
            open_keys = _open_keys()
            if (ac, ticker, opposite) in open_keys:
                # Reversal close couldn't be priced — don't open the opposite yet.
                log.warning(
                    f"Paper skip: unpriced reversal for {ticker}; not opening {direction}"
                )
                continue

        if (ac, ticker, direction) in open_keys:
            continue  # dedup: a position for this call is already open
        if ac == "crypto":
            symbol = resolve_kraken_pair(ticker, crypto_overrides)
        else:
            symbol = resolve_stooq_symbol(ticker, cache, overrides)
        if not symbol:
            log.warning(f"Paper skip: no instrument for {ticker} ({ac})")
            continue
        price = fetch_price(ac, symbol)
        if price is None:
            log.warning(f"Paper skip: no price for {ticker} ({symbol})")
            continue
        book["positions"].append(
            {
                "id": f"{today}:{ac}:{ticker}:{direction}",
                "opened": today,
                "asset_class": ac,
                "venue": _VENUE_BY_ASSET.get(ac, ""),
                "execution": "paper",
                "ticker": ticker,
                "instrument": symbol,
                "play_type": None,
                "direction": direction,
                "confidence": s.get("confidence"),
                "topic": s.get("topic"),
                "thesis_ref": s.get("thesis_ref"),
                "rationale": s.get("rationale"),
                "entry_price": price,
                "entry_date": today,
                "status": "open",
                "close_reason": None,
                "closed_date": None,
                "checkpoints": {},
                "last_mark": None,
                "realized_return": None,
            }
        )
        open_keys.add((ac, ticker, direction))
        opened += 1

    save_book(book)
    log.info(f"Opened {opened} paper position(s)")


def mark_to_market(book: dict, today_str: str) -> dict:
    """Mark every open position to market, record crossed horizon checkpoints, close at 4w.

    Mutates and returns the book. A position whose Stooq price can't be fetched is left open
    and retried next run. All crossed-but-unrecorded checkpoints are recorded in one pass
    (covers a missed weekly run); closing happens once the 4w checkpoint is recorded.
    """
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        price = price_position(p)
        if price is None:
            log.warning(f"MtM kept open (no price): {p['ticker']} ({p['instrument']})")
            continue
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        for label, threshold in PAPER_HORIZONS.items():
            if label not in p["checkpoints"] and days_open >= threshold:
                p["checkpoints"][label] = {
                    "date": today_str,
                    "price": price,
                    "return": ret,
                }
        if PAPER_CLOSE_HORIZON in p["checkpoints"]:
            p["status"] = "closed"
            p["close_reason"] = "horizon"
            p["closed_date"] = today_str
            p["realized_return"] = p["checkpoints"][PAPER_CLOSE_HORIZON]["return"]
    return book


def paper_scorecard(book: dict) -> str:
    """Build a Telegram-HTML paper scorecard: hit-rate and mean returns (percentages only)."""
    positions = book.get("positions", [])
    closed = [p for p in positions if p["status"] == "closed"]
    open_ = [p for p in positions if p["status"] == "open"]

    def _hit_rate(ps):
        rets = [
            p["realized_return"] for p in ps if p.get("realized_return") is not None
        ]
        if not rets:
            return None
        hits = sum(1 for r in rets if r > 0)
        return 100.0 * hits / len(rets), len(rets)

    lines = ["<b>🧪 PAPER SIGNALS SCORECARD</b>"]
    overall = _hit_rate(closed)
    if overall:
        rate, n = overall
        lines.append(f"• Realized hit-rate (at close): {rate:.0f}% of {n}")
        for conf in ("high", "medium"):
            sub = _hit_rate([p for p in closed if p.get("confidence") == conf])
            if sub:
                lines.append(f"  – {conf}: {sub[0]:.0f}% of {sub[1]}")
    for label in PAPER_HORIZONS:
        rets = [
            p["checkpoints"][label]["return"]
            for p in positions
            if label in p.get("checkpoints", {})
        ]
        if rets:
            lines.append(
                f"• Mean {label} return: {100.0 * sum(rets) / len(rets):+.1f}% (n={len(rets)})"
            )
    lines.append(f"• Open: {len(open_)} | Closed: {len(closed)}")
    recent = sorted(closed, key=lambda p: p.get("closed_date") or "", reverse=True)[:5]
    if recent:
        lines.append("Recently closed:")
        for p in recent:
            r = p.get("realized_return")
            rstr = f"{100 * r:+.1f}%" if r is not None else "n/a"
            lines.append(
                f"  • {p['ticker']} {p['direction']}: {rstr} ({p['close_reason']})"
            )
    return "\n".join(lines)
