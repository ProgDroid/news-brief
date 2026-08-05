#!/usr/bin/env python3
"""Equity paper-trading layer: ticker resolution + multi-provider pricing, the
paper book, return math, position close, and the open/mark-to-market functions.
Imports infra from common.py. (Later phases generalise this to a
multi-asset subsystem.)"""

import json
import re
import requests
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

import common
from common import (
    DATA_DIR,
    SIGNALS_DIR,
    log,
    _write_json_atomic,
    _load_json_or,
    file_lock,
    T212_API_KEY_ID,
    T212_API_KEY,
    T212_BASE_URL,
    t212_auth_header,
    MODEL,
    ANTHROPIC_HEADERS,
    POLYGRAM_EMAIL,
    POLYGRAM_PASSWORD,
    HAIRCUT_BPS_EQUITY,
    HAIRCUT_BPS_CRYPTO,
    HAIRCUT_BPS_PREDICTION,
    VOL_SPIKE_MULT,
    VOL_TRAILING_N,
    VOL_MIN_SAMPLES,
    VOL_ALERT_COOLDOWN_HRS,
    VOL_FLOOR_EQUITY,
    VOL_FLOOR_CRYPTO,
    VOL_FLOOR_PREDICTION,
)

PAPER_DIR = DATA_DIR / "paper"
BOOK_FILE = PAPER_DIR / "book.json"
# Generous timeout for the paper-book lock: mode_paper holds it across the whole
# load->open->save span, which (when PolyGram is configured) can include the
# creds-gated Claude prediction matcher. A coincident /close that can't acquire
# within this window degrades to "command failed, retry", never to corruption.
BOOK_LOCK_TIMEOUT = 120.0
# Legacy equity-only book; read once and migrated into BOOK_FILE on first load.
LEGACY_PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"
CRYPTO_TICKER_MAP_FILE = PAPER_DIR / "crypto_ticker_map.json"
WATCHLIST_FILE = PAPER_DIR / "watchlist.json"
VOLUME_HISTORY_FILE = (
    PAPER_DIR / "volume-history.json"
)  # used by the Phase 5 volume monitor (Task 5)
LEAKAGE_LOG_FILE = PAPER_DIR / "leakage-log.json"  # Stage A: declined-signal counts

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

# Market-pulse instruments. Each entry: (label, asset_class, instrument).
# Tier 1 — macro spine, always fetched (universal risk-on/off pulse).
MARKET_SPINE = [
    ("S&P 500", "index", "^GSPC"),
    ("US Dollar (DXY)", "index", "DX-Y.NYB"),
    ("Gold", "index", "GC=F"),
    ("Bitcoin", "crypto", "XBTUSD"),
]
# Tier 2 — pin-derived: only fetched for currently pinned topics. Gaps allowed
# (a pin with no clean instrument simply contributes no market line).
PIN_INSTRUMENTS = {
    "iran": [("Brent crude", "index", "BZ=F")],
    "japan": [("USD/JPY", "index", "USDJPY=X"), ("Nikkei 225", "index", "^N225")],
    "china": [("Hang Seng", "index", "^HSI")],
}

# ── PolyGram (prediction markets) ─────────────────────────────────────────────
POLYGRAM_BASE = "https://polygram.ink/api"
POLYGRAM_TOKEN_FILE = PAPER_DIR / "polygram_token.json"
PG_CANDIDATE_CAP = 25  # max candidate markets fed to the matcher (prompt-size bound)
PG_SIMILARITY_FLOOR = 0.60  # open only matches at/above this matcher similarity
PG_PER_QUERY_CAP = (
    5  # max NEW markets any one search token contributes (junk-recall bound)
)
PG_MIN_TOKEN_LEN = 4  # PolyGram /search is substring-based; short tokens over-match
PG_MAX_HOLD_DAYS = 182  # ~26w backstop close for never-resolving resolution markets


class Quote(NamedTuple):
    """A single instrument's daily quote. open_ and volume may be None when the
    provider omits them; callers treat a None field as 'unavailable' and skip. A
    provider that can't supply a usable close returns None instead of a Quote."""

    close: float
    open_: float | None
    volume: float | None


# Neutral instrument symbol is '<base>.<market>' with market in this set.
_MARKETS = {"us", "uk", "de", "fr"}


def _parse_symbol(symbol: str) -> tuple[str, str] | None:
    """Split a neutral instrument symbol ('aapl.us') into (base, market).

    Returns None when the string has no recognised market suffix — callers skip.
    """
    base, _, market = symbol.rpartition(".")
    if not base or market not in _MARKETS:
        return None
    return base, market


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


def fetch_kraken_price(pair: str) -> float | None:
    """Fetch the last-trade price for a Kraken pair (e.g. 'XBTUSD') via the public API.

    Returns None on network error, a non-empty Kraken error array, an empty/garbled
    result, or a non-positive price — callers MUST treat None as 'could not price' and
    skip, never substitute a guessed value.
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


def _kraken_closes(pair: str, since: str) -> dict[str, float]:
    """Daily closes for a Kraken pair from `since` onward, via /0/public/OHLC.

    interval=1440 = daily candles; OHLC close is field index 4. Kraken keys the
    result by canonical pair name plus a 'last' int, so take the single list-valued
    entry. Returns {date: close}, or {} on error array / empty / parse failure —
    callers fall back to the current mark price.
    """
    try:
        since_ts = int(
            datetime.strptime(since, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return {}
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440&since={since_ts}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken history failed for {pair}: {e}")
        return {}
    if data.get("error"):
        log.warning(f"Kraken history error for {pair}: {data['error']}")
        return {}
    result = data.get("result") or {}
    candles = next(
        (v for k, v in result.items() if k != "last" and isinstance(v, list)), None
    )
    if not candles:
        return {}
    out: dict[str, float] = {}
    for row in candles:
        try:
            date = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            out[date] = float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def fetch_daily_move(asset_class: str, instrument: str) -> float | None:
    """Intraday percent move (open → last) for one instrument, single fetch.

    Equity → multi-provider (Alpaca/Yahoo) quote (open → close); index → a raw
    Yahoo symbol (market-pulse indices/commodities/FX: '^GSPC', 'GC=F', 'USDJPY=X',
    …) fetched directly, bypassing the base.market resolver; crypto → Kraken Ticker
    (o=today's open, c[0]=last). Returns the percent change rounded to 2dp, or None
    on any failure / non-positive open — callers render '—', never guess.
    """
    if asset_class == "crypto":
        return _kraken_daily_move(instrument)
    q = _yahoo_fetch(instrument) if asset_class == "index" else fetch_quote(instrument)
    if q is None or q.open_ is None or q.open_ <= 0:
        return None
    return round((q.close - q.open_) / q.open_ * 100, 2)


def _kraken_daily_move(pair: str) -> float | None:
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken move fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        return None
    result = data.get("result") or {}
    if not result:
        return None
    entry = next(iter(result.values()))
    try:
        open_, last = float(entry["o"]), float(entry["c"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if open_ <= 0:
        return None
    return round((last - open_) / open_ * 100, 2)


def fetch_kraken_volume(pair: str) -> float | None:
    """Last-24h traded volume for a Kraken pair (Ticker 'v' = [today, last 24h]).
    None on error / garbled result — caller skips (mirrors fetch_kraken_price)."""
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Kraken volume fetch failed for {pair}: {e}")
        return None
    if data.get("error"):
        log.warning(f"Kraken error for {pair}: {data['error']}")
        return None
    result = data.get("result") or {}
    if not result:
        log.warning(f"Kraken returned no result for {pair}")
        return None
    entry = next(iter(result.values()))
    try:
        vol = float(entry["v"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return vol if vol >= 0 else None


# Yahoo market suffixes. US is bare; the rest carry an exchange suffix.
_YAHOO_SUFFIX = {"us": "", "uk": ".L", "de": ".DE", "fr": ".PA"}

# A browser UA is enough for the keyless v8/chart endpoint (no crumb/cookie needed).
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _yahoo_format_symbol(base: str, market: str) -> str:
    """Translate a neutral (base, market) to a Yahoo ticker ('rr','uk' -> 'RR.L')."""
    return f"{base.upper()}{_YAHOO_SUFFIX.get(market, '')}"


def _yahoo_fetch(yahoo_symbol: str) -> Quote | None:
    """Fetch a daily Quote for an already-formatted Yahoo symbol via v8/chart.

    Handles the GBp (pence) -> GBP (pounds) /100 conversion via the response's
    currency field. Returns None on any network/parse failure or non-positive
    close — callers treat None as 'could not price' and skip.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "range": "1d"},
            headers=_YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Yahoo fetch failed for {yahoo_symbol}: {e}")
        return None
    result = (data.get("chart") or {}).get("result")
    if not result:
        log.warning(f"Yahoo returned no result for {yahoo_symbol}")
        return None
    node = result[0]
    meta = node.get("meta") or {}
    try:
        quote = node["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return None

    def _last(key):  # last usable (non-null, numeric) entry in the series, else None
        for v in reversed(quote.get(key) or []):
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return None

    close = _last("close")
    if close is None:
        rmp = meta.get("regularMarketPrice")
        try:
            close = float(rmp) if rmp is not None else None
        except (TypeError, ValueError):
            close = None
    if close is None or close <= 0:
        log.warning(f"Yahoo returned no usable close for {yahoo_symbol}")
        return None
    open_ = _last("open")
    volume = _last("volume")
    # GBp is pence; convert price fields (not volume) to the major unit.
    if meta.get("currency") == "GBp":
        close /= 100.0
        if open_ is not None:
            open_ /= 100.0
    return Quote(close=close, open_=open_, volume=volume)


def _yahoo_closes(yahoo_symbol: str, start: str, end: str) -> dict[str, float]:
    """Daily closes for an already-formatted Yahoo symbol over [start, end] inclusive.

    Same v8/chart endpoint as _yahoo_fetch, but a date range (period1/period2).
    Returns {date: close} with the GBp->GBP /100 conversion applied, or {} on any
    network/parse failure — callers fall back to the current mark price.
    """
    try:
        p1 = int(
            datetime.strptime(start, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        # +1 day so the end date itself falls inside Yahoo's half-open range.
        p2 = int(
            (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return {}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "period1": p1, "period2": p2},
            headers=_YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Yahoo history failed for {yahoo_symbol}: {e}")
        return {}
    result = (data.get("chart") or {}).get("result")
    if not result:
        return {}
    node = result[0]
    meta = node.get("meta") or {}
    timestamps = node.get("timestamp") or []
    try:
        closes = node["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return {}
    pence = meta.get("currency") == "GBp"
    out: dict[str, float] = {}
    for ts, c in zip(timestamps, closes):
        if c is None:
            continue
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        if pence:
            val /= 100.0
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out[date] = val
    return out


def _yahoo_quote(base: str, market: str) -> Quote | None:
    """Daily Quote for a neutral (base, market) from Yahoo."""
    return _yahoo_fetch(_yahoo_format_symbol(base, market))


def _alpaca_quote(symbol: str) -> Quote | None:
    """Daily Quote for a US symbol from Alpaca's snapshot endpoint (free IEX feed).

    Returns None when Alpaca is unconfigured or on any network/parse failure /
    non-positive close — callers skip (mirrors the other pricers). US-only: the
    free feed has no international listings, so the router only routes US here.
    """
    if not common.ALPACA_API_KEY_ID or not common.ALPACA_API_SECRET:
        return None
    url = f"{common.ALPACA_DATA_URL}/v2/stocks/{symbol}/snapshot"
    headers = {
        "APCA-API-KEY-ID": common.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": common.ALPACA_API_SECRET,
    }
    try:
        resp = requests.get(url, headers=headers, params={"feed": "iex"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Alpaca fetch failed for {symbol}: {e}")
        return None
    bar = data.get("dailyBar") or data.get("prevDailyBar") or {}
    try:
        close = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        log.warning(f"Alpaca returned no usable close for {symbol}")
        return None
    if close <= 0:
        return None

    def _opt(key):
        v = bar.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return Quote(close=close, open_=_opt("o"), volume=_opt("v"))


def fetch_quote(base_or_symbol: str, market: str | None = None) -> Quote | None:
    """Daily Quote for an instrument, routed by market with failover.

    Accepts either (base, market) or a single neutral symbol ('aapl.us'). US
    routes Alpaca -> Yahoo; UK/DE/FR route Yahoo only. Returns the first non-None
    provider result, or None when nothing prices it (callers skip, never guess).
    """
    if market is None:
        parsed = _parse_symbol(base_or_symbol)
        if parsed is None:
            return None
        base, market = parsed
    else:
        base = base_or_symbol
    if market not in _MARKETS:
        return None
    if market == "us":
        return _alpaca_quote(base.upper()) or _yahoo_quote(base, market)
    return _yahoo_quote(base, market)


def fetch_benchmark() -> float | None:
    """S&P 500 level: Yahoo ^GSPC (true index) -> Alpaca SPY ETF (degraded proxy).
    None when neither prices — caller treats as benchmark unavailable."""
    q = _yahoo_fetch("^GSPC") or _alpaca_quote("SPY")
    return q.close if q else None


def fetch_pg_volume(market_id: str) -> float | None:
    """24h volume for a PolyGram market, read from market detail (volume lives on
    the market object in the Polymarket mirror, not in the price series). None if
    the market is unfetchable or exposes no volume field — caller skips. This is
    the spec's graceful-degradation path for prediction volume."""
    m = polygram_market(market_id)
    if m is None:
        return None
    for field in ("volume24hr", "volume24Hr", "volume"):
        v = m.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def fetch_volume(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument's volume via the fetcher for its asset class."""
    if asset_class == "crypto":
        return fetch_kraken_volume(instrument)
    if asset_class == "prediction":
        return fetch_pg_volume(instrument)
    q = fetch_quote(instrument)
    return q.volume if q else None


def fetch_benchmark_level(asset_class: str) -> float | None:
    """Current benchmark index level for an asset class (best-effort, None on failure).

    Equity → S&P 500 (multi-provider benchmark); crypto → BTC/XBT (Kraken);
    prediction → None (naive coin-flip baseline, handled at close as
    benchmark_return=0).
    """
    if asset_class == "equity":
        return fetch_benchmark()
    if asset_class == "crypto":
        pair = resolve_kraken_pair("BTC", load_crypto_ticker_overrides())
        return fetch_kraken_price(pair) if pair else None
    return None


def _fetch_pg_half_spread(token_id: str) -> float | None:
    """Best bid/ask half-spread as a fraction of mid, from PolyGram's orderbook.

    Returns None when uncredentialed/unavailable/malformed. Tolerant of both
    [{"price": "0.4"}] and [["0.4", size]] level shapes.
    """
    data = _polygram_get(f"/orderbook/{token_id}")
    if not isinstance(data, dict):
        return None

    def _best(levels):
        if not levels:
            return None
        lvl = levels[0]
        try:
            raw = lvl["price"] if isinstance(lvl, dict) else lvl[0]
            return float(raw)
        except (TypeError, ValueError, KeyError, IndexError):
            return None

    bid = _best(data.get("bids"))
    ask = _best(data.get("asks"))
    if bid is None or ask is None or ask < bid:
        return None
    mid = (ask + bid) / 2
    if mid <= 0:
        return None
    return (ask - bid) / 2 / mid


def _sleeve_a_entry_ok(held_price: float, token_id: str) -> bool:
    """True iff the held-side price is in the favorite band AND the live half-spread
    is readable and within the gate. Unreadable orderbook ⇒ False (fail-closed).

    Rejections log their inputs. This is the narrowest gate on the live path, and
    without the numbers "nothing traded today" is indistinguishable from "several
    markets missed the band by a cent" — the log is how the caps get tuned.
    """
    if not (common.PG_A_BAND_LO <= held_price <= common.PG_A_BAND_HI):
        log.info(
            f"Sleeve A gate: price={held_price:.3f} outside "
            f"band={common.PG_A_BAND_LO}-{common.PG_A_BAND_HI}"
        )
        return False
    half = _fetch_pg_half_spread(token_id)
    if half is None:
        log.info(f"Sleeve A gate: orderbook unreadable for {token_id} (fail-closed)")
        return False
    if half > common.PG_A_SPREAD_GATE:
        log.info(
            f"Sleeve A gate: half_spread={half:.3f} > gate={common.PG_A_SPREAD_GATE} "
            f"(price={held_price:.3f})"
        )
        return False
    return True


def _stamp_open_benchmark(p: dict) -> None:
    """Stamp benchmark_entry (+ prediction entry_spread) on a freshly opened position.

    Best-effort: any fetch failure leaves the field None and never raises.
    """
    ac = p.get("asset_class", "equity")
    if ac == "prediction":
        p["benchmark_entry"] = None
        tok = p.get("token_id")
        try:
            p["entry_spread"] = _fetch_pg_half_spread(tok) if tok else None
        except Exception as e:
            log.warning(f"PG spread fetch failed for {p.get('ticker')}: {e}")
            p["entry_spread"] = None
        return
    p["entry_spread"] = None
    try:
        p["benchmark_entry"] = fetch_benchmark_level(ac)
    except Exception as e:
        log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
        p["benchmark_entry"] = None


def _haircut_fraction(p: dict) -> float:
    """Round-trip cost fraction for a closed position, by asset class.

    Prediction: entry = real orderbook half-spread if captured at open, else the
    config fallback; resolution settles at 1/0 (entry leg only), momentum adds a
    config-bps exit leg. Equity/crypto: a single config round-trip constant.
    """
    ac = p.get("asset_class", "equity")
    if ac == "prediction":
        entry = p.get("entry_spread")
        if entry is None:
            entry = HAIRCUT_BPS_PREDICTION / 10_000
        if p.get("play_type") == "momentum":
            return entry + HAIRCUT_BPS_PREDICTION / 10_000
        return entry
    if ac == "crypto":
        return HAIRCUT_BPS_CRYPTO / 10_000
    return HAIRCUT_BPS_EQUITY / 10_000


def _stamp_close_metrics(p: dict, day: str) -> None:
    """Stamp haircut/net_return/benchmark_return/edge on a just-closed position.

    Call AFTER status is set to "closed" and realized_return is stamped. Best-effort
    on the benchmark fetch — never raises out of a close path. A fetch failure or a
    legacy position with no benchmark_entry leaves benchmark_return/edge None, but
    net_return is always set. `day` is currently unused (the benchmark is marked at
    the live level, not an as-of-`day` historical level); retained for call-site
    symmetry and a possible future as-of lookup.
    """
    gross = p.get("realized_return")
    if gross is None:
        return
    haircut = _haircut_fraction(p)
    p["haircut"] = haircut
    net = gross - haircut
    p["net_return"] = net
    ac = p.get("asset_class", "equity")
    bench = None
    if ac == "prediction":
        bench = 0.0  # naive coin-flip baseline
    else:
        entry = p.get("benchmark_entry")
        if entry:
            try:
                level = fetch_benchmark_level(ac)
            except Exception as e:
                log.warning(f"Benchmark fetch failed for {p.get('ticker')}: {e}")
                level = None
            if level is not None:
                bench = _signal_return("bullish", entry, level)
    p["benchmark_return"] = bench
    p["edge"] = (net - bench) if bench is not None else None


def _parse_pg_market(m: dict) -> dict | None:
    """Flatten a raw PolyGram market into the fields the seam needs.

    PolyGram mirrors Polymarket: `outcomes`, `outcomePrices`, and `clobTokenIds`
    are JSON-ENCODED STRINGS of index-aligned arrays (YES=index 0, NO=index 1).
    Returns None if the required arrays are missing or unparseable — callers skip.

    `outcomes` is parsed separately and never fatal: it is needed only to LABEL a
    side (see _pg_outcome_label), so a market with readable prices and tokens must
    stay tradeable-shaped even if its labels are junk.
    """
    try:
        prices = [float(x) for x in json.loads(m["outcomePrices"])]
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (KeyError, TypeError, ValueError):
        return None
    if len(prices) < 2:
        return None
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
    except (TypeError, ValueError):
        outcomes = []
    return {
        "market_id": str(m.get("id", "")),
        "question": str(m.get("question", "")),
        "prices": prices,
        "yes_price": prices[0],
        "outcomes": outcomes if isinstance(outcomes, list) else [],
        "end_date": m.get("endDate"),
        "closed": bool(m.get("closed")),
        "uma_status": m.get("umaResolutionStatus"),
        "token_ids": token_ids,
    }


def _pg_outcome_label(parsed: dict, side_index: int) -> str:
    """The venue's OWN label for a side, falling back to Yes/No.

    `POST /trade/place` validates `outcome` against the market's `outcomes` array,
    so a hardcoded "Yes"/"No" is a 400 on any binary that isn't labelled that way —
    Up/Down, Above/Below, two candidate names. Polymarket-mirrored markets are
    usually Yes/No, which is why nothing caught this until the first real order:
    every read path keys off side_index and never needed the label.
    """
    outs = parsed.get("outcomes") or []
    if side_index < len(outs):
        label = outs[side_index]
        if isinstance(label, str) and label.strip():
            return label.strip()
    return "Yes" if side_index == 0 else "No"


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


def _pg_market_volume(m: dict) -> float:
    """Best-effort 24h volume for ranking a raw PolyGram market (0.0 if absent)."""
    for key in ("volume24hr", "volumeNum", "volume"):
        v = m.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _signal_search_terms(signals: list) -> list[str]:
    """Distinct lowercased entity tokens from signal topics, in first-seen order.

    PolyGram's /search is a substring match, so we feed it short specific tokens
    (e.g. 'hormuz', 'china', 'iran') rather than the kebab-case topic slug, which
    never appears verbatim in a market title. Tokens shorter than PG_MIN_TOKEN_LEN
    are dropped (a 2-char token substring-matches half the catalogue), and the
    signal `ticker` is deliberately NOT searched ('MU' substring-matches 'Musk').
    """
    terms: list[str] = []
    for s in signals:
        for tok in re.split(r"[^a-z0-9]+", (s.get("topic") or "").lower()):
            if len(tok) >= PG_MIN_TOKEN_LEN and tok not in terms:
                terms.append(tok)
    return terms


def _gather_pg_candidates(signals: list) -> list:
    """Search PolyGram for markets related to the day's signals; dedup + cap.

    Searches each distinct entity token derived from the signal topics (see
    _signal_search_terms), keeps OPEN binary markets ranked by 24h volume, and
    takes at most PG_PER_QUERY_CAP per token so no single token monopolises the
    pool. Dedups by market_id and caps the total at PG_CANDIDATE_CAP to bound the
    matcher prompt. Returns the parsed-candidate dicts (market_id/question/
    yes_price/end_date/event_id) the matcher is shown; event_id is the /search
    event wrapper's id, carried through for the live (Sleeve A) open path.
    """
    seen: dict[str, dict] = {}
    for q in _signal_search_terms(signals):
        raw: list[dict] = []
        for ev in polygram_search(q) or []:
            if isinstance(ev, dict):
                ev_id = ev.get("id") or ev.get("eventId")  # live-verify key name
                for m in ev.get("markets", []):
                    if isinstance(m, dict):
                        m["_event_id"] = ev_id  # stash for parse/open
                        raw.append(m)
        raw.sort(key=_pg_market_volume, reverse=True)
        taken = 0
        for m in raw:
            if taken >= PG_PER_QUERY_CAP:
                break
            parsed = _parse_pg_market(m)
            if parsed is None or parsed["closed"]:
                continue
            if parsed["market_id"] not in seen:
                seen[parsed["market_id"]] = {
                    "market_id": parsed["market_id"],
                    "question": parsed["question"],
                    "yes_price": parsed["yes_price"],
                    "end_date": parsed["end_date"],
                    "event_id": m.get("_event_id"),
                }
                taken += 1
        if len(seen) >= PG_CANDIDATE_CAP:
            break
    return list(seen.values())


def _parse_matches(text: str, candidate_ids: set) -> list:
    """Parse the matcher's JSON-array reply, validating each match.

    Resilient like the signals parser: locate the array within any surrounding
    prose/fences, json.loads it, and keep only well-formed matches whose
    market_id is a real candidate. Returns [] on any failure.
    """
    try:
        arr = json.loads(text[text.index("[") : text.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("market_id", ""))
        side = str(item.get("side", "")).upper()
        play = str(item.get("play_type", "")).lower()
        if (
            mid not in candidate_ids
            or side not in ("YES", "NO")
            or play not in ("resolution", "momentum")
        ):
            continue
        try:
            sim = float(item.get("similarity"))
        except (TypeError, ValueError):
            continue
        target = item.get("target")
        try:
            target = float(target) if target is not None else None
        except (TypeError, ValueError):
            target = None
        out.append(
            {
                "market_id": mid,
                "side": side,
                "play_type": play,
                "similarity": sim,
                "target": target,
            }
        )
    return out


def run_prediction_matcher(signals: list, candidates: list) -> list:
    """One synchronous Claude call mapping signals → prediction-market matches.

    Same Messages-API shape as run_dig but with NO tools/web search. Returns the
    validated match list (possibly empty); never raises into the cron path.
    """
    if not candidates:
        return []
    payload = {
        "model": MODEL,
        "max_tokens": 2048,
        # Raw-JSON extraction on a tight 2048 budget; disable thinking (adaptive is
        # the Sonnet 5 default) so it can't crowd out the JSON array and truncate it.
        "thinking": {"type": "disabled"},
        "system": (
            "You map daily investing signals to live prediction markets. Given today's "
            "signals and candidate markets, return ONLY a JSON array (no prose, no code "
            'fences). Each element: {"market_id": str, "side": "YES"|"NO", "play_type": '
            '"resolution"|"momentum", "similarity": number 0..1, "target": number|null}. '
            "side is the outcome the signal implies. play_type is 'resolution' when the "
            "signal speaks to the eventual settled outcome, 'momentum' when it is a "
            "near-term catalyst likely to move the odds regardless of settlement. target "
            "(momentum only, else null) is an optional held-side price in 0..1 to take "
            "profit at. similarity is your confidence the signal is genuinely about this "
            "market. Omit weak matches; return [] if none."
        ),
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Today's signals:\n{json.dumps(signals)}\n\n"
                    f"Candidate markets:\n{json.dumps(candidates)}\n\n"
                    "Return the JSON array of matches."
                ),
            }
        ],
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
    except Exception as e:
        log.warning(f"Prediction matcher call failed: {e}")
        return []
    return _parse_matches(text, {c["market_id"] for c in candidates})


def fetch_price(asset_class: str, instrument: str) -> float | None:
    """Mark one instrument to market via the pricer for its asset class.

    Equity → multi-provider (Alpaca/Yahoo) pricer, crypto → Kraken. Returns None
    on any pricing failure — callers skip, never guess.
    """
    if asset_class == "crypto":
        return fetch_kraken_price(instrument)
    q = fetch_quote(instrument)
    return q.close if q else None


def historical_closes(
    asset_class: str, instrument: str, start: str, end: str
) -> dict[str, float]:
    """Daily close series for one position over [start, end], routed by asset class.

    equity → Yahoo (base.market resolved); index → Yahoo (raw symbol);
    crypto → Kraken OHLC. Prediction and anything unrecognised → {}. The map feeds
    _snap_close to price a checkpoint at its true crossing date; {} → the caller
    falls back to the current mark price.
    """
    if asset_class == "crypto":
        return _kraken_closes(instrument, start)
    if asset_class == "index":
        return _yahoo_closes(instrument, start, end)
    if asset_class == "equity":
        parsed = _parse_symbol(instrument)
        if parsed is None:
            return {}
        return _yahoo_closes(_yahoo_format_symbol(*parsed), start, end)
    return {}


def price_position(p: dict) -> float | None:
    """Mark a position to market by dispatching on its asset_class.

    Equity/crypto go through fetch_price (instrument-level). Prediction marks the
    held side from market detail (outcomePrices[side_index]) — None if unfetchable.
    """
    if p.get("asset_class") == "prediction":
        m = polygram_market(p["instrument"])
        if m is None:
            return None
        parsed = _parse_pg_market(m)
        if parsed is None:
            return None
        return parsed["prices"][p["side_index"]]
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


def _stooq_suffix_for(meta: dict) -> str | None:
    """Stooq market suffix for a T212 instrument, from its currency (ISIN country
    for EUR). Returns None when the currency maps to no known market."""
    ccy = (meta.get("currencyCode") or "").upper()
    suffix = _STOOQ_SUFFIX.get(ccy)
    if suffix is None and ccy == "EUR":
        suffix = _STOOQ_EUR_BY_ISIN.get((meta.get("isin") or "")[:2].upper())
    return suffix


def _instrument_base_keys(tkr: str, meta: dict) -> set[str]:
    """The upper-case base forms a signal may carry for this T212 ticker.

    Always the raw base (segment before the first '_'). For the two-part LSE/Xetra
    form ('RRl_EQ', 'EXV1d_EQ') the exchange-marker letter is baked INTO that base
    segment, so a plain signal ('RR', 'EXV1') would never match — also accept the
    marker-stripped base. Additive (both 'RRL' and 'RR'), so a base whose final
    letter is genuinely part of the symbol still matches on its raw form.
    """
    parts = tkr.split("_")
    base = parts[0].upper()
    keys = {base}
    if len(parts) == 2:
        marker = _STOOQ_MARKET_MARKER.get(_stooq_suffix_for(meta) or "")
        if marker and base.endswith(marker.upper()):
            keys.add(base[: -len(marker)])
    return keys


def _match_instrument_by_base(
    symbol: str, instruments: dict
) -> tuple[str, dict] | None:
    """Find the cached T212 instrument whose base symbol matches `symbol`.

    Signals carry plain exchange symbols ('SHEL'), while T212 tickers are
    '<SYMBOL>_<COUNTRY>_EQ' (US) or '<SYMBOLl>_EQ' (LSE). The base is the part
    before the first '_'. When several listings share a base, prefer the US one
    (signals use US-style symbols). Returns (matched_ticker, metadata) — the
    matched T212 ticker is the authoritative form for marker-stripping — or None.
    """
    want = symbol.split("_")[0].upper()
    candidates = []
    for tkr, meta in instruments.items():
        if want not in _instrument_base_keys(tkr, meta):
            continue
        parts = tkr.split("_")
        country = parts[1].upper() if len(parts) > 2 else ""
        candidates.append((_COUNTRY_PREFERENCE.get(country, 9), tkr, meta))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][1], candidates[0][2]


def resolve_symbol(ticker: str, cache: dict, overrides: dict) -> str | None:
    """Map a signal ticker to a neutral instrument symbol (e.g. 'aapl.us').

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
    # Derive base + marker from the MATCHED T212 ticker, not the input. A plain
    # signal can carry the marker baked in ('SGLNl', no underscore) yet base-match
    # the 2-segment LSE listing 'SGLNl_EQ'; using the matched ticker is what lets
    # the marker get stripped in that case (input-based parsing kept the 'l').
    meta = instruments.get(ticker)
    matched = ticker  # exact-match path: the input already IS the T212 ticker
    if meta is None:
        found = _match_instrument_by_base(ticker, instruments)
        if found is None:
            return None
        matched, meta = found
    parts = matched.split("_")
    base = parts[0].lower()
    suffix = _stooq_suffix_for(meta)
    if suffix is None:
        return None
    # The two-part LSE/Xetra form ('RRl_EQ', 'SGLNl_EQ', 'EXV1d_EQ') carries a
    # trailing market-marker letter; strip it. The three-part US/country form
    # ('AAPL_US_EQ') is clean, so a US-resolved symbol is never stripped.
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


def _record_leakage(date_str: str, tally: dict) -> None:
    """Best-effort merge of one run's directional-signal leakage tally into the log.

    Keyed by date so the weekly report can sum a trailing window. A write/parse
    failure is logged and skipped — leakage accounting never aborts a paper run.
    """
    try:
        data = _load_json_or(LEAKAGE_LOG_FILE, {})
        if not isinstance(data, dict):
            data = {}
        data[date_str] = tally
        _write_json_atomic(LEAKAGE_LOG_FILE, data)
    except Exception as e:
        log.warning(f"Leakage log write skipped: {e}")


def load_watchlist() -> dict:
    """Cross-asset watch list: {"items": [{raw, asset_class, instrument, added?}]}."""
    return _load_json_or(WATCHLIST_FILE, {"items": []})


def save_watchlist(wl: dict) -> None:
    _write_json_atomic(WATCHLIST_FILE, wl)


def resolve_watch_entry(token: str, asset_class: str | None = None) -> dict | None:
    """Resolve a /watch token into a watchlist entry, or None if unresolvable.

    Inference order is crypto → equity. Prediction is never inferred (a market
    can't be guessed from a ticker-shaped token): it needs an explicit class and a
    market id, validated via polygram_market.
    """
    token = token.strip()
    if asset_class == "prediction":
        if polygram_market(token) is None:
            return None
        return {"raw": token, "asset_class": "prediction", "instrument": token}
    if asset_class in (None, "crypto"):
        pair = resolve_kraken_pair(token, load_crypto_ticker_overrides())
        if pair:
            return {"raw": token, "asset_class": "crypto", "instrument": pair}
        if asset_class == "crypto":
            return None
    sym = resolve_symbol(token, load_instruments_cache(), load_ticker_overrides())
    if sym:
        return {"raw": token, "asset_class": "equity", "instrument": sym}
    return None


def _watched_instruments() -> list[tuple[str, str]]:
    """Union of watchlist entries and OPEN-position instruments, deduped by
    (asset_class, instrument). Entries missing either field are skipped rather
    than crashing the sweep (mirrors the codebase's corruption-resilient posture)."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for it in load_watchlist().get("items", []):
        ac, inst = it.get("asset_class"), it.get("instrument")
        if not ac or not inst:
            continue
        key = (ac, inst)
        if key not in seen:
            seen.add(key)
            out.append(key)
    for p in load_book().get("positions", []):
        if p.get("status") != "open":
            continue
        inst = p.get("instrument")
        if not inst:
            continue
        key = (p.get("asset_class", "equity"), inst)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def run_volume_monitor(now: datetime | None = None) -> list[str]:
    """Sweep watched instruments + open positions for volume anomalies.

    Returns a list of Telegram-HTML alert strings (empty when quiet). Updates
    volume-history.json (trailing samples + last_alert_ts) in place. Each
    instrument is isolated: a fetch returning None or raising is logged and
    skipped without aborting the sweep. `now` is injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    hist = _load_json_or(VOLUME_HISTORY_FILE, {})
    alerts: list[str] = []
    for asset_class, instrument in _watched_instruments():
        key = f"{asset_class}:{instrument}"
        entry = hist.get(key) or {"samples": [], "last_alert_ts": None}
        try:
            current = fetch_volume(asset_class, instrument)
        except Exception as e:
            log.warning(f"Volume fetch raised for {key}: {e}")
            current = None
        if current is None:
            continue
        prior = entry.get("samples", [])
        is_spike, ratio = _volume_anomaly(prior, current, _floor_for(asset_class))
        if is_spike and not _in_cooldown(entry.get("last_alert_ts"), now):
            baseline = sum(prior) / len(prior)
            alerts.append(
                _format_alert(asset_class, instrument, current, baseline, ratio)
            )
            entry["last_alert_ts"] = now.isoformat()
        entry["samples"] = _append_sample(prior, current)
        hist[key] = entry
    _write_json_atomic(VOLUME_HISTORY_FILE, hist)
    return alerts


def build_market_pulse(pins: list[str]) -> str:
    """Assemble the 'what moved & why' block: macro spine + pin-derived
    instruments + open-position moves + current volume anomalies. Pure data —
    the model supplies the 'why'. Best-effort: a failed fetch renders '—' and
    never raises into the brief pipeline.
    """
    seen: set[tuple[str, str]] = set()
    instruments: list[tuple[str, str, str]] = []
    for label, ac, inst in MARKET_SPINE:
        if (ac, inst) not in seen:
            seen.add((ac, inst))
            instruments.append((label, ac, inst))
    for topic in pins:
        for label, ac, inst in PIN_INSTRUMENTS.get(topic, []):
            if (ac, inst) not in seen:
                seen.add((ac, inst))
                instruments.append((label, ac, inst))

    def _fmt(move: float | None) -> str:
        return f"{move:+.1f}%" if move is not None else "—"

    lines = ["### MARKET PULSE — WHAT MOVED (open→last)"]
    for label, ac, inst in instruments:
        try:
            move = fetch_daily_move(ac, inst)
        except Exception as e:
            log.warning(f"Market pulse move failed for {inst}: {e}")
            move = None
        lines.append(f"- {label}: {_fmt(move)}")

    pos_lines = []
    for p in load_book().get("positions", []):
        if p.get("status") != "open":
            continue
        inst = p.get("instrument")
        if not inst:
            continue
        ac = p.get("asset_class", "equity")
        try:
            move = fetch_daily_move(ac, inst)
        except Exception:
            move = None
        pos_lines.append(f"- {p.get('ticker', inst)}: {_fmt(move)}")
    if pos_lines:
        lines.append("\n### YOUR POSITIONS — TODAY'S MOVE")
        lines.extend(pos_lines)

    now = datetime.now(timezone.utc)
    hist = _load_json_or(VOLUME_HISTORY_FILE, {})
    anomalies = []
    for key, entry in hist.items():
        ts = entry.get("last_alert_ts")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if now - when < timedelta(days=2):
            anomalies.append(f"- {key}: recent volume spike ({ts[:10]})")
    if anomalies:
        lines.append("\n### VOLUME ANOMALIES (last alerts)")
        lines.extend(anomalies)

    return "\n".join(lines)


def _signal_return(direction: str, entry: float, price: float) -> float:
    """Directional return ratio: +1 for bullish, -1 for bearish, FX/unit-neutral."""
    sign = 1.0 if direction == "bullish" else -1.0
    return sign * (price / entry - 1.0)


def _volume_anomaly(prior: list, current: float, floor: float) -> tuple[bool, float]:
    """(is_spike, ratio) for current volume vs the mean of prior samples.

    Pure. Warm-up (need >= VOL_MIN_SAMPLES prior samples), an absolute floor, and a
    positive baseline all gate the ratio test, so a cold start or a thin instrument
    never alerts.
    """
    if len(prior) < VOL_MIN_SAMPLES or current < floor:
        return (False, 0.0)
    baseline = sum(prior) / len(prior)
    if baseline <= 0:
        return (False, 0.0)
    ratio = current / baseline
    return (ratio >= VOL_SPIKE_MULT, ratio)


def _append_sample(samples: list, current: float) -> list:
    """Append current to the trailing window, deduping a consecutive duplicate
    (daily-resolution equity volume is identical all day -> one sample/day, not 24),
    capped at VOL_TRAILING_N most-recent samples."""
    if samples and samples[-1] == current:
        return samples
    return (samples + [current])[-VOL_TRAILING_N:]


def _in_cooldown(last_alert_ts: str | None, now: datetime) -> bool:
    """True if the last alert for this instrument is within VOL_ALERT_COOLDOWN_HRS."""
    if not last_alert_ts:
        return False
    try:
        last = datetime.fromisoformat(last_alert_ts)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < timedelta(hours=VOL_ALERT_COOLDOWN_HRS)


def _floor_for(asset_class: str) -> float:
    return {
        "equity": VOL_FLOOR_EQUITY,
        "crypto": VOL_FLOOR_CRYPTO,
        "prediction": VOL_FLOOR_PREDICTION,
    }.get(asset_class, 0.0)


def _fmt_vol(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


def _format_alert(
    asset_class: str, instrument: str, current: float, baseline: float, ratio: float
) -> str:
    return (
        f"📈 <b>{asset_class}</b> {instrument}: {ratio:.1f}× avg volume "
        f"({_fmt_vol(current)} vs {_fmt_vol(baseline)})"
    )


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
    _stamp_close_metrics(p, day)
    return True


def _pg_match_pass(signals: list) -> tuple[list, list]:
    """Search PolyGram and matcher-rank the day's signals once: (candidates, matches).

    This is the expensive step of the prediction seam — one PolyGram search per
    entity token plus a single Claude matcher call — and it is deliberately run
    ONCE per collect and shared by the paper and live open paths.

    Sharing is a correctness requirement, not just a cost saving: the matcher is
    nondeterministic, so two passes over the same signals can return different
    match sets. When they diverge, a paper row for a market is no evidence that
    Sleeve A ever considered it, and the paper book stops being a usable read on
    what the live sleeve is doing.
    """
    candidates = _gather_pg_candidates(signals)
    if not candidates:
        return [], []
    return candidates, run_prediction_matcher(signals, candidates)


def _open_prediction_positions(
    book: dict, signals: list, today: str, open_keys: set, match_pass=None
) -> int:
    """Match the day's signals to live PolyGram markets and open paper positions.

    Creds-gated (no-op when PolyGram is unconfigured, so unit tests never hit the
    network). Opens one long-the-held-side position per match above the similarity
    floor, deduped by market, priced at the live held-side mark. Returns the count.

    `match_pass` is the shared (candidates, matches) tuple from _pg_match_pass;
    when omitted the function runs its own pass, which keeps it usable standalone.
    """
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        log.info("PolyGram not configured — skipping prediction matching")
        return 0
    candidates, matches = (
        match_pass if match_pass is not None else _pg_match_pass(signals)
    )
    if not candidates:
        log.info("No PolyGram candidates today")
        return 0
    opened = 0
    for mt in matches:
        if mt["similarity"] < PG_SIMILARITY_FLOOR:
            continue
        mid, side = mt["market_id"], mt["side"]
        key = ("prediction", mid, "bullish")
        if key in open_keys:
            continue  # dedup: a position for this market is already open
        m = polygram_market(mid)
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None or parsed["closed"]:
            log.warning(f"Prediction skip: market {mid} unfetchable/closed")
            continue
        side_index = 0 if side == "YES" else 1
        price = parsed["prices"][side_index]
        if price is None or price <= 0:
            log.warning(f"Prediction skip: non-positive price for {mid} ({side})")
            continue
        play_type = mt["play_type"]
        book["positions"].append(
            {
                "id": f"{today}:prediction:{mid}:{side}",
                "opened": today,
                "asset_class": "prediction",
                "venue": "polygram",
                "execution": "paper",
                "ticker": mid,
                "instrument": mid,
                "play_type": play_type,
                "outcome": _pg_outcome_label(parsed, side_index),
                "side_index": side_index,
                "token_id": parsed["token_ids"][side_index]
                if len(parsed["token_ids"]) > side_index
                else None,
                "target": mt["target"] if play_type == "momentum" else None,
                "direction": "bullish",  # always long the held side (long-sense return)
                "confidence": None,
                "topic": parsed["question"],
                "thesis_ref": None,
                "rationale": f"matched (similarity={mt['similarity']})",
                "source_id": None,
                "source_kind": "unknown",
                "source_perspective": None,
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
        _stamp_open_benchmark(book["positions"][-1])
        open_keys.add(key)
        opened += 1
    return opened


def _sleeve_b_open_ok(book, market_id, outcome, amount):
    """Sleeve-B guard: per-position cap, total-sleeve cap, and NO-DCA. Returns (ok, reason)."""
    if amount <= 0 or amount > common.PG_B_POS_CAP:
        return False, f"over per-position cap (${common.PG_B_POS_CAP:g})"
    exposure = sum(
        (p.get("cost_basis") or 0.0)
        for p in book.get("positions", [])
        if p.get("execution") == "live"
        and p.get("sleeve") == "B"
        and p.get("status") == "open"
    )
    if exposure + amount > common.PG_B_TOTAL_CAP:
        return False, f"over total Sleeve-B cap (${common.PG_B_TOTAL_CAP:g})"
    for p in book.get("positions", []):
        if (
            p.get("execution") == "live"
            and p.get("sleeve") == "B"
            and p.get("status") == "open"
            and p.get("instrument") == market_id
            and p.get("outcome") == outcome
        ):
            return False, "a Sleeve-B position already open on this market (no DCA)"
    return True, ""


def score_settled_theses() -> int:
    """Grade unscored theses whose live row has settled. Returns count scored.

    outcome_result = 1 if the held side won (realized_return > 0) else 0.
    brier = (p_hat - outcome_result)**2 when p_hat is known, else None."""
    log_ = common.load_thesis_log()
    if not log_:
        return 0
    ret_by_id = {
        p["id"]: p.get("realized_return")
        for p in load_book().get("positions", [])
        if p.get("status") == "closed" and p.get("realized_return") is not None
    }
    n = 0
    for rec in log_:
        if rec.get("scored"):
            continue
        ret = ret_by_id.get(rec.get("id"))
        if ret is None:
            continue
        outcome = 1 if ret > 0 else 0
        rec["outcome_result"] = outcome
        rec["brier"] = (
            None if rec.get("p_hat") is None else (rec["p_hat"] - outcome) ** 2
        )
        rec["scored"] = True
        n += 1
    if n:
        common.save_thesis_log(log_)
    return n


_SLEEVE_A_BLOCKED_CAP = 3  # near-miss markets carried into the daily message


def open_sleeve_a_live(book, signals, today, match_pass=None) -> dict:
    """Open real-money Sleeve-A favorite-fade positions. Creds+flag gated.

    For each match in the shared pass: buy the held favorite side only when
    it is in-band and tight-spread (via _sleeve_a_entry_ok) and an eventId is known, sized
    at PG_A_STAKE, deduped against open live rows. All opens route through the fail-closed
    polygram_live.open_live_position (kill-switch + cap enforced there).

    Returns a STATUS DICT rather than a bare count. The sleeve is fail-closed by
    design, so zero opens is its ordinary output, and a count cannot separate
    "nothing was in band" (working as intended) from "the orderbook read failed"
    (a fault) — the gates are only distinguishable at the moment they fire. The dict
    is rendered into the daily Telegram message by validation.daily_trade_message so
    the reason reaches the operator instead of only a container log.

    Keys: state ("off" | "no_creds" | "no_candidates" | "ran"), live_enabled,
    a_enabled, candidates, matches, opened, skips {reason: count}, wallet (the
    balance the bot can actually READ — None means unreadable, itself the fault,
    since cap_ok then rejects every order), blocked (near-miss markets with their
    numbers, capped at _SLEEVE_A_BLOCKED_CAP).

    `match_pass` is the shared (candidates, matches) tuple from _pg_match_pass —
    the SAME set the paper path judged, so the two books stay comparable.
    """
    import polygram_live

    status = {
        "state": "off",
        "live_enabled": bool(common.PG_LIVE_ENABLED),
        "a_enabled": bool(common.PG_A_ENABLED),
        "candidates": 0,
        "matches": 0,
        "opened": 0,
        "skips": {},
        "wallet": None,
        "blocked": [],
    }
    if not (common.PG_LIVE_ENABLED and common.PG_A_ENABLED):
        log.info(
            f"Sleeve A live: off (PG_LIVE_ENABLED={common.PG_LIVE_ENABLED}, "
            f"PG_A_ENABLED={common.PG_A_ENABLED})"
        )
        return status
    if not (POLYGRAM_EMAIL and POLYGRAM_PASSWORD):
        log.warning(
            "Sleeve A live: enabled but PolyGram credentials are missing "
            "(POLYGRAM_EMAIL/POLYGRAM_PASSWORD)"
        )
        status["state"] = "no_creds"
        return status
    candidates, matches = (
        match_pass if match_pass is not None else _pg_match_pass(signals)
    )
    status["candidates"], status["matches"] = len(candidates), len(matches)
    if not candidates:
        status["state"] = "no_candidates"
        log.info("Sleeve A live: no open PolyGram candidates for today's signals")
        return status
    status["state"] = "ran"
    # Report the balance the bot can READ, not the one on the site. cap_ok treats an
    # unreadable wallet as "unfunded" and rejects silently, so a None here is the
    # whole explanation for a day with no orders.
    try:
        status["wallet"] = polygram_live.wallet_balance()
    except Exception as e:  # a diagnostic read must never break the open path
        log.warning(f"Sleeve A live: wallet read failed: {e}")
    ev_by_mid = {c["market_id"]: c.get("event_id") for c in candidates}
    live_open = {
        (p.get("instrument"), p.get("outcome"))
        for p in book["positions"]
        if p.get("execution") == "live" and p.get("status") == "open"
    }
    exposure = sum(
        (p.get("cost_basis") or 0.0)
        for p in book["positions"]
        if p.get("execution") == "live" and p.get("status") == "open"
    )
    opened = 0
    skips: dict[str, int] = status["skips"]
    blocked: list[dict] = status["blocked"]

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for mt in matches:
        if mt["similarity"] < PG_SIMILARITY_FLOOR:
            skip("below_similarity")
            continue
        mid, side = mt["market_id"], mt["side"]
        event_id = ev_by_mid.get(mid)
        if not event_id:
            log.warning(f"Sleeve A skip: no eventId for {mid}")
            skip("no_event_id")
            continue
        m = polygram_market(mid)
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None:
            skip("unreadable")  # a fault: the market fetch or its shape failed
            continue
        if parsed["closed"]:
            skip("market_closed")  # ordinary: the market settled since the search
            continue
        side_index = 0 if side == "YES" else 1
        if (
            len(parsed["prices"]) <= side_index
            or len(parsed["token_ids"]) <= side_index
        ):
            skip("side_missing")
            continue
        # The venue's own label, so the order body and the (market, outcome) keys the
        # venue answers with agree. Costs one market fetch before the dedup check,
        # which is the price of not inventing the label.
        outcome = _pg_outcome_label(parsed, side_index)
        if (mid, outcome) in live_open:
            skip("already_open")
            continue
        held_price = parsed["prices"][side_index]
        token_id = parsed["token_ids"][side_index]
        if held_price is None:
            skip("no_price")
            continue
        if not _sleeve_a_entry_ok(held_price, token_id):
            # Split the gate's verdict without a second orderbook fetch: the band is
            # pure arithmetic, so in-band-but-rejected can only be the spread or an
            # unreadable book. That distinction is design-working vs something broken.
            in_band = common.PG_A_BAND_LO <= held_price <= common.PG_A_BAND_HI
            why = "spread_or_book" if in_band else "out_of_band"
            skip(why)
            if len(blocked) < _SLEEVE_A_BLOCKED_CAP:
                blocked.append(
                    {
                        "question": parsed.get("question") or mid,
                        "price": held_price,
                        "why": why,
                    }
                )
            continue
        row = polygram_live.open_live_position(
            book,
            sleeve="A",
            event_id=event_id,
            market_id=mid,
            token_id=token_id,
            outcome=outcome,
            side_index=side_index,
            amount=common.PG_A_STAKE,
            topic=parsed.get("question"),
            source_id=None,
            source_kind="unknown",
            source_perspective=None,
            live_exposure=exposure,
        )
        if row is None:
            # cap_ok / kill-switch / non-fill — those paths log their own detail.
            skip("open_rejected")
            continue
        live_open.add((mid, outcome))
        exposure += common.PG_A_STAKE
        opened += 1
    status["opened"] = opened
    log.info(
        f"Sleeve A live: {len(candidates)} candidate(s), {len(matches)} match(es), "
        f"{opened} opened" + (f"; skipped {skips}" if skips else "")
    )
    return status


def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Equity/crypto: each medium/high-confidence directional signal with a resolvable
    instrument opens one notional position (deduped per asset_class+ticker+direction),
    priced via Stooq/Kraken. Prediction: the Claude matcher maps ALL of today's signals
    to live PolyGram markets (creds-gated) and opens long-the-held-side positions.
    Unmappable/unpriced/macro signals are skipped and logged. MtM + close run weekly.

    The whole load->open->save span runs under the book lock so a concurrent /close
    (commands mode) can't clobber this collect run's writes (see BOOK_LOCK_TIMEOUT).

    Returns {"opened": int, "sleeve_a": status|None} — the caller renders the Sleeve-A
    status into the daily Telegram message, since the live sleeve's reasons for not
    trading are invisible in the book by construction.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return {"opened": 0, "sleeve_a": None}

    signals = json.loads(snap_path.read_text()).get("signals", [])
    if not signals:
        log.info("No signals today — nothing to paper-trade")
        return {"opened": 0, "sleeve_a": None}

    leakage = {
        "traded": 0,
        "no_ticker": 0,
        "low_confidence": 0,
        "neutral": 0,
        "no_instrument": 0,
        "no_price": 0,
        "unpriced_reversal": 0,
    }
    actionable = []
    for s in signals:
        if s.get("direction") not in ("bullish", "bearish"):
            leakage["neutral"] += 1
        elif s.get("confidence") not in ("medium", "high"):
            leakage["low_confidence"] += 1
        elif not s.get("ticker"):
            leakage["no_ticker"] += 1
        else:
            actionable.append(s)

    with file_lock(BOOK_FILE, timeout=BOOK_LOCK_TIMEOUT):
        book = load_book()

        def _open_keys() -> set:
            return {
                (p.get("asset_class", "equity"), p["ticker"], p["direction"])
                for p in book["positions"]
                if p["status"] == "open"
            }

        open_keys = _open_keys()
        opened = 0

        if actionable:
            cache = refresh_instruments_cache()
            overrides = load_ticker_overrides()
            crypto_overrides = load_crypto_ticker_overrides()

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
                        leakage["unpriced_reversal"] += 1
                        continue

                if (ac, ticker, direction) in open_keys:
                    continue  # dedup: a position for this call is already open
                if ac == "crypto":
                    symbol = resolve_kraken_pair(ticker, crypto_overrides)
                else:
                    symbol = resolve_symbol(ticker, cache, overrides)
                if not symbol:
                    log.warning(f"Paper skip: no instrument for {ticker} ({ac})")
                    leakage["no_instrument"] += 1
                    continue
                price = fetch_price(ac, symbol)
                if price is None:
                    log.warning(f"Paper skip: no price for {ticker} ({symbol})")
                    leakage["no_price"] += 1
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
                        "source_id": s.get("source_id"),
                        "source_kind": s.get("source_kind", "unknown"),
                        "source_perspective": s.get("source_perspective"),
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
                _stamp_open_benchmark(book["positions"][-1])
                open_keys.add((ac, ticker, direction))
                leakage["traded"] += 1
                opened += 1
        else:
            log.info("No actionable equity/crypto signals today")

        # One search + one matcher call, shared by both open paths (see _pg_match_pass).
        # Creds-guarded here so an unconfigured host never touches the network.
        match_pass = (
            _pg_match_pass(signals)
            if (POLYGRAM_EMAIL and POLYGRAM_PASSWORD)
            else ([], [])
        )
        opened += _open_prediction_positions(
            book, signals, today, open_keys, match_pass
        )

        sleeve_a = None
        try:
            sleeve_a = open_sleeve_a_live(book, signals, today, match_pass)
            opened += sleeve_a["opened"]
        except Exception as e:  # live path is non-load-bearing for the paper run
            # Report the crash as a state rather than leaving sleeve_a None: a
            # swallowed exception and a correctly-declining sleeve both produce zero
            # live rows, and without this the message simply omits the sleeve — the
            # exact ambiguity the status dict exists to remove.
            log.exception("Sleeve A live open failed")
            sleeve_a = {"state": "crashed", "error": f"{type(e).__name__}: {e}"}

        save_book(book)
        _record_leakage(today, leakage)
        log.info(f"Opened {opened} paper position(s)")
        return {"opened": opened, "sleeve_a": sleeve_a}


def _snap_close(closes: dict[str, float], target_date: str) -> float | None:
    """Close on the last trading day on/before target_date (None if none <= target).

    Lexical comparison is valid for ISO YYYY-MM-DD. Snaps a weekend/holiday
    crossing date back to the prior available close; returns None when the map is
    empty or every date is after target (target precedes available history).
    """
    candidates = [d for d in closes if d <= target_date]
    if not candidates:
        return None
    return closes[max(candidates)]


def _has_new_crossing(p: dict, days_open: int) -> bool:
    """True if any horizon has crossed but isn't recorded yet — gates the fetch."""
    return any(
        label not in p["checkpoints"] and days_open >= threshold
        for label, threshold in PAPER_HORIZONS.items()
    )


def _record_checkpoints(
    p: dict,
    today_str: str,
    price: float,
    ret: float,
    days_open: int,
    closes: dict[str, float],
):
    """Record any crossed-but-unrecorded horizon checkpoints (idempotent, one pass).

    Each checkpoint is priced at the close on its true crossing date
    (entry_date + threshold) when available in `closes` (price_basis="historical");
    otherwise it falls back to the current mark price/return (price_basis="current").
    `closes` is {} for prediction and for runs where nothing newly crossed.
    """
    entry = datetime.strptime(p["entry_date"], "%Y-%m-%d").date()
    for label, threshold in PAPER_HORIZONS.items():
        if label in p["checkpoints"] or days_open < threshold:
            continue
        crossing = (entry + timedelta(days=threshold)).strftime("%Y-%m-%d")
        hist = _snap_close(closes, crossing)
        if hist is not None:
            p["checkpoints"][label] = {
                "date": crossing,
                "price": hist,
                "return": _signal_return(p["direction"], p["entry_price"], hist),
                "price_basis": "historical",
            }
        else:
            p["checkpoints"][label] = {
                "date": today_str,
                "price": price,
                "return": ret,
                "price_basis": "current",
            }


def mark_to_market(book: dict, today_str: str) -> dict:
    """Mark every open position to market, record crossed horizon checkpoints, close on trigger.

    Mutates and returns the book. Equity/crypto: record 1w/2w/4w checkpoints, close at 4w.
    Prediction: dispatched to _mtm_prediction (held-side mark + play_type close trigger).
    A position whose price can't be fetched is left open and retried next run.
    """
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        if p.get("execution") == "live":
            continue  # live rows exit via the hourly sweep, never the weekly measurement path
        if p.get("asset_class") == "prediction":
            _mtm_prediction(p, today, today_str)
            continue
        price = price_position(p)
        if price is None:
            log.warning(f"MtM kept open (no price): {p['ticker']} ({p['instrument']})")
            continue
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        closes = (
            historical_closes(
                p.get("asset_class", "equity"),
                p["instrument"],
                p["entry_date"],
                today_str,
            )
            if _has_new_crossing(p, days_open)
            else {}
        )
        _record_checkpoints(p, today_str, price, ret, days_open, closes)
        if PAPER_CLOSE_HORIZON in p["checkpoints"]:
            p["status"] = "closed"
            p["close_reason"] = "horizon"
            p["closed_date"] = today_str
            p["realized_return"] = p["checkpoints"][PAPER_CLOSE_HORIZON]["return"]
            _stamp_close_metrics(p, today_str)
    return book


def _settle_prediction(p: dict, day: str, ret: float, reason: str):
    """Close a prediction position with the given return and reason.

    The mark price is intentionally not a parameter: the caller has already
    written p["last_mark"] before settling, so this only flips status/return.
    """
    p["status"] = "closed"
    p["close_reason"] = reason
    p["closed_date"] = day
    p["realized_return"] = ret
    _stamp_close_metrics(p, day)


def _mtm_prediction(p: dict, today, today_str: str):
    """Mark + (maybe) close one open prediction position from a single market-detail fetch.

    Held-side mark = outcomePrices[side_index]; return is long-sense (you hold the token).
    Close trigger forks by play_type:
      momentum  → close at target-cross (held price >= target) else 4w horizon backstop.
      resolution→ hold to settlement (closed & uma 'resolved'); PG_MAX_HOLD_DAYS backstop.
    Left open (retried next run) if the market can't be fetched/parsed.
    """
    m = polygram_market(p["instrument"])
    parsed = _parse_pg_market(m) if m is not None else None
    if parsed is None:
        log.warning(f"MtM kept open (no price): prediction {p['instrument']}")
        return
    price = parsed["prices"][p["side_index"]]
    ret = _signal_return(
        "bullish", p["entry_price"], price
    )  # always long the held side
    p["last_mark"] = {"date": today_str, "price": price, "return": ret}
    days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
    _record_checkpoints(p, today_str, price, ret, days_open, {})

    if p["play_type"] == "resolution":
        if parsed["closed"] and parsed["uma_status"] == "resolved":
            _settle_prediction(p, today_str, ret, "settlement")
        elif days_open >= PG_MAX_HOLD_DAYS:
            _settle_prediction(p, today_str, ret, "max_hold")
    else:  # momentum
        target = p.get("target")
        if target is not None and price >= target:
            _settle_prediction(p, today_str, ret, "target")
        elif PAPER_CLOSE_HORIZON in p["checkpoints"]:
            _settle_prediction(p, today_str, ret, "horizon")


def _sleeve_a_exit_reason(held_price, entry_price, days_open, days_to_end):
    """Decide a live favorite-fade exit. take > stop > time_stop; near-dated holds to settlement."""
    if held_price >= common.PG_A_TAKE:
        return "take"
    if entry_price - held_price >= common.PG_A_STOP:
        return "stop"
    if days_to_end is not None and days_to_end <= common.PG_A_NEAR_DAYS:
        return None  # ride near-dated to settlement (reconcile handles it)
    if days_open >= common.PG_A_TIME_STOP_DAYS:
        return "time_stop"
    return None


def sweep_live_exits(book, today) -> int:
    """Hourly exit sweep for open live Sleeve-A rows. Marks from the live market and closes
    via polygram_live.close_live_position on take/stop/time-stop. Caller holds the book lock."""
    import polygram_live

    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    closed = 0
    for p in book.get("positions", []):
        if (
            p.get("execution") != "live"
            or p.get("sleeve") != "A"
            or p.get("status") != "open"
        ):
            continue
        m = polygram_market(p["instrument"])
        parsed = _parse_pg_market(m) if m is not None else None
        if parsed is None:
            log.warning(f"Sleeve A sweep: no price for {p.get('id')}; kept open")
            continue
        si = p["side_index"]
        if len(parsed["prices"]) <= si or parsed["prices"][si] is None:
            continue
        held = parsed["prices"][si]
        days_open = (
            today_d - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()
        ).days
        days_to_end = None
        end = parsed.get("end_date") or p.get("end_date")
        if end:
            try:
                days_to_end = (
                    datetime.strptime(str(end)[:10], "%Y-%m-%d").date() - today_d
                ).days
            except ValueError:
                days_to_end = None
        reason = _sleeve_a_exit_reason(held, p["entry_price"], days_open, days_to_end)
        if reason and polygram_live.close_live_position(p, reason):
            closed += 1
    return closed
