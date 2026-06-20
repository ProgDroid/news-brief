# enrichment/universe.py
"""Derive the per-run enrichment query set from existing artifacts.

Reads book.json positions, watchlist.json items, the latest signals snapshot and
feedback pins — no new coupling. Single stocks -> per-symbol bundles; known ETFs
-> a thematic search (ETFs resolve as funds, so company sentiment is meaningless);
pins -> themes.

v1 SCOPE NOTE: the design spec also listed "the brief's Top Stories" as a theme
source. Those don't exist at submit time (they ARE the batch output), so
Top-Stories-driven thematic enrichment is deferred to a possible collect-time
fast-follow. v1 themes = pins + ETF-watchlist themes."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Known ETF base symbols from the live watchlist -> theme string. ETFs resolve as
# funds (no company sentiment), so they feed a thematic search instead of a
# per-symbol bundle. (Memory: bigdata-evaluation-and-trading-split.)
_ETF_THEME_MAP = {
    "DXJG": "Japan equities and yen",
    "DXJ": "Japan equities and yen",
    "EXV1": "European banks",
    "KSTR": "China STAR / China tech",
    "ARMG": "defence",
    "SGLN": "gold",
    "VEUA": "European equities",
    "SPOL": "Polish equities",
}

_VENUE_SUFFIX_RE = re.compile(r"_(?:[A-Z]{2}_)?EQ$")  # _US_EQ, _EQ


@dataclass(frozen=True)
class Universe:
    tickers: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)


def normalize_ticker(raw: str) -> str:
    """Reduce a raw/instrument token to the base symbol Bigdata.com resolves on."""
    t = (raw or "").strip().upper()
    t = _VENUE_SUFFIX_RE.sub("", t)
    # Re-apply marker strip on the case-preserving original tail: a single trailing
    # lowercase l/d is an LSE/Xetra marker; uppercase L/D is part of the symbol.
    tail = (raw or "").strip()
    tail = re.sub(r"_(?:[A-Za-z]{2}_)?[Ee][Qq]$", "", tail)
    if tail and tail[-1] in ("l", "d") and tail[:-1].isupper():
        t = tail[:-1].upper()
    # Collapse any stray underscore the suffix strip leaves behind, e.g. the
    # live book's double-underscore "AVAV__US_EQ" -> "AVAV_" -> "AVAV".
    return t.strip("_")


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def latest_signal_tickers(signals_dir: Path) -> list[str]:
    snaps = sorted(Path(signals_dir).glob("signals-*.json"))
    if not snaps:
        return []
    try:
        data = json.loads(snaps[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _dedup(
        normalize_ticker(s["ticker"])
        for s in data.get("signals") or []
        if s.get("ticker")
    )


def _pos_ticker(p: dict) -> str:
    return p.get("ticker") or p.get("instrument", "")


def build_universe(
    book: dict,
    watchlist: dict,
    signal_tickers: list[str],
    pins: list[str],
) -> Universe:
    raw_symbols: list[str] = []
    etf_themes: list[str] = []

    for p in book.get("positions", []):
        if p.get("status") == "open" and p.get("asset_class") == "equity":
            raw_symbols.append(_pos_ticker(p))

    for it in watchlist.get("items", []):
        if it.get("asset_class") != "equity":
            continue
        raw_symbols.append(it.get("raw") or it.get("instrument", ""))

    raw_symbols.extend(signal_tickers)

    tickers: list[str] = []
    for raw in raw_symbols:
        base = normalize_ticker(raw)
        if not base:
            continue
        if base in _ETF_THEME_MAP:
            etf_themes.append(_ETF_THEME_MAP[base])
        else:
            tickers.append(base)

    themes = _dedup([*pins, *etf_themes])
    return Universe(tickers=_dedup(tickers), themes=themes)
