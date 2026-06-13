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
)

PAPER_DIR = DATA_DIR / "paper"
PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"

PAPER_HORIZONS = {"1w": 7, "2w": 14, "4w": 28}  # days from entry_date
PAPER_CLOSE_HORIZON = "4w"  # close the position once this checkpoint is recorded

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


def load_paper_book() -> dict:
    return _load_json_or(PAPER_BOOK_FILE, {"positions": []})


def save_paper_book(book: dict):
    _write_json_atomic(PAPER_BOOK_FILE, book)


def _signal_return(direction: str, entry: float, price: float) -> float:
    """Directional return ratio: +1 for bullish, -1 for bearish, FX/unit-neutral."""
    sign = 1.0 if direction == "bullish" else -1.0
    return sign * (price / entry - 1.0)


def _close_position_at_market(p: dict, day: str, reason: str) -> bool:
    """Close one open position at the current Stooq mark, stamping realized_return.

    Shared by the weekly horizon close, the /close command, and reversal closes.
    Returns False (leaving the position open) when Stooq can't price it.
    """
    price = fetch_stooq_price(p["stooq_symbol"])
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

    Each medium/high-confidence directional signal with a resolvable ticker opens one notional
    paper position (deduped per ticker+direction). Prices come from Stooq; unmappable tickers,
    Stooq 'N/D', and macro/null-ticker signals are skipped and logged. Marking-to-market and
    closing happen in the weekly job.
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

    book = load_paper_book()
    open_keys = {
        (p["ticker"], p["direction"])
        for p in book["positions"]
        if p["status"] == "open"
    }
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()

    opened = 0
    for s in actionable:
        ticker, direction = s["ticker"], s["direction"]
        opposite = "bearish" if direction == "bullish" else "bullish"

        # Reversal: a fresh opposite-direction call closes the standing position first.
        if (ticker, opposite) in open_keys:
            for p in book["positions"]:
                if (
                    p["status"] == "open"
                    and p["ticker"] == ticker
                    and p["direction"] == opposite
                    and _close_position_at_market(p, today, "reversal")
                ):
                    log.info(f"Paper reversal: closed {ticker} {opposite}")
            open_keys = {
                (q["ticker"], q["direction"])
                for q in book["positions"]
                if q["status"] == "open"
            }
            if (ticker, opposite) in open_keys:
                # Reversal close couldn't be priced — don't open the opposite yet.
                log.warning(
                    f"Paper skip: unpriced reversal for {ticker}; not opening {direction}"
                )
                continue

        if (ticker, direction) in open_keys:
            continue  # dedup: a position for this call is already open
        symbol = resolve_stooq_symbol(ticker, cache, overrides)
        if not symbol:
            log.warning(f"Paper skip: no Stooq symbol for {ticker}")
            continue
        price = fetch_stooq_price(symbol)
        if price is None:
            log.warning(f"Paper skip: no price for {ticker} ({symbol})")
            continue
        book["positions"].append(
            {
                "id": f"{today}:{ticker}:{direction}",
                "opened": today,
                "ticker": ticker,
                "stooq_symbol": symbol,
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
        open_keys.add((ticker, direction))
        opened += 1

    save_paper_book(book)
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
        price = fetch_stooq_price(p["stooq_symbol"])
        if price is None:
            log.warning(
                f"MtM kept open (no price): {p['ticker']} ({p['stooq_symbol']})"
            )
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
