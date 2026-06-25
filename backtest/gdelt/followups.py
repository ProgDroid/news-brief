"""Follow-up robustness analyses on the cached 2022-2024 GDELT data.
Reuses tested primitives; operator-run (network for yfinance prices only;
GDELT read from .gdelt_cache). Single parse pass -> multiple region buckets.

1. Regime: per-year IC of Mideast conflict_mentions vs USO/GLD (is -0.22 a 2022 artifact?)
2. Normalize + tighten: conflict SHARE of total volume, and Hormuz-chokepoint region, vs USO/GLD
3. Broaden instruments: Mideast conflict_mentions vs VIX/BNO/UNG/GDX
4. Different theatre: Russia-Ukraine conflict_mentions vs UNG/USO/WEAT
"""

from collections import defaultdict
from datetime import date, timedelta

from backtest.align import align_dated
from backtest.gdelt.aggregate import GdeltDaily, aggregate_events, merge_daily
from backtest.gdelt.events import parse_row
from backtest.gdelt.fetch import fetch_day_rows
from backtest.gdelt.snap import snap_forward, to_sentiment_series
from backtest.metrics import hit_rate, spearman_rank_ic
from backtest.prices_yf import fetch_price_series
from backtest.returns import forward_returns
from backtest.run import run_backtest
from backtest.series import SentimentPoint, SentimentSeries

BROAD = frozenset({"IR", "IZ", "IS", "SA", "SY", "YM", "LE"})
CHOKE = frozenset({"IR", "SA", "IZ", "YM"})  # Hormuz + Bab-el-Mandeb oil chokepoints
UKR = frozenset({"RS", "UP"})  # Russia, Ukraine (FIPS)
START, END = "2022-01-01", "2024-12-31"
PRICE_END = "2025-01-05"
HORIZONS = [1, 3, 5, 10]
CACHE = ".gdelt_cache"


def daterange(start, end):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [
        (d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range((d1 - d0).days + 1)
    ]


def _merge_into(acc, d, rec):
    acc[d] = merge_daily(acc[d], rec, date_iso=d) if d in acc else rec


def build_all():
    broad, choke, ukr, total = {}, {}, {}, {}
    n_files = 0
    for ymd in daterange(START, END):
        rows = fetch_day_rows(ymd, cache_dir=CACHE)
        if not rows:
            continue
        n_files += 1
        evs = [e for e in map(parse_row, rows) if e]
        by_date = defaultdict(list)
        for e in evs:
            by_date[e.date].append(e)
        for d, des in by_date.items():
            _merge_into(broad, d, aggregate_events(d, des, BROAD))
            _merge_into(choke, d, aggregate_events(d, des, CHOKE))
            _merge_into(ukr, d, aggregate_events(d, des, UKR))
            total[d] = total.get(d, 0.0) + sum(e.num_mentions for e in des)
    print(f"parsed {n_files} cached files; broad-region days={len(broad)}")
    return broad, choke, ukr, total


def series_from_map(m, label="sig"):
    return SentimentSeries(
        label, tuple(SentimentPoint(d, v) for d, v in sorted(m.items()))
    )


def snap_floats(m, cal):
    daily = {d: GdeltDaily(d, v, 0, 0, 0.0, 0.0, v) for d, v in m.items()}
    return {d: g.conflict_mentions for d, g in snap_forward(daily, cal).items()}


def region_series(region_daily, cal, label="sig"):
    return to_sentiment_series(
        snap_forward(region_daily, cal), "conflict_mentions", label=label
    )


def share_series(region_daily, total_map, cal, label="share"):
    rs = snap_forward(region_daily, cal)
    ts = snap_floats(total_map, cal)
    m = {d: g.conflict_mentions / ts[d] for d, g in rs.items() if ts.get(d, 0.0) > 0}
    return series_from_map(m, label)


def cell(series, sym, prices, mode="level"):
    res = run_backtest(
        {sym: series}, {sym: prices}, HORIZONS, mode=mode, standardize=True
    )
    c = res["confirmation"]
    if not c:
        return f"  {sym:5s} {mode:5s}: no held-out data"
    return (
        f"  {sym:5s} {mode:5s}: best_h={res['best_horizon']:>2}  "
        f"held-out IC={c['ic']:+.4f}  hit={c['hit_rate']:.1%}  n={c['n']}"
    )


def main():
    broad, choke, ukr, total = build_all()

    # Prices (cached per symbol).
    syms = ["USO", "GLD", "SPY", "ITA", "^VIX", "BNO", "UNG", "GDX", "WEAT"]
    prices = {}
    for s in syms:
        try:
            prices[s] = fetch_price_series(s, START, PRICE_END)
        except Exception as e:  # noqa: BLE001
            print(f"price fetch failed {s}: {e}")
    cals = {s: set(p.closes) for s, p in prices.items()}

    print("\n=== 1. REGIME: per-year rank IC, Mideast conflict_mentions (level) ===")
    print("    (is the strong USO -0.22 persistent or a 2022/Ukraine-regime artifact?)")
    for sym in ("USO", "GLD"):
        if sym not in prices:
            continue
        s = region_series(broad, cals[sym])
        for h in (5, 10):
            rows = align_dated(s, forward_returns(prices[sym], [h]), h)
            by_yr = defaultdict(list)
            for d, sig, ret in rows:
                by_yr[d[:4]].append((sig, ret))
            full_ic = spearman_rank_ic([(sig, ret) for _, sig, ret in rows])
            yr_str = "  ".join(
                f"{yr}:IC={spearman_rank_ic(p):+.3f}(hit={hit_rate(p):.0%},n={len(p)})"
                for yr, p in sorted(by_yr.items())
            )
            print(f"  {sym} h={h:>2}  FULL IC={full_ic:+.4f}  ||  {yr_str}")

    print("\n=== 2. NORMALIZE + TIGHTEN REGION vs USO/GLD (held-out, level) ===")
    for sym in ("USO", "GLD"):
        if sym not in prices:
            continue
        print(
            f" [{sym}] raw broad:   "
            + cell(region_series(broad, cals[sym]), sym, prices[sym]).strip()
        )
        print(
            f" [{sym}] share/vol:   "
            + cell(share_series(broad, total, cals[sym]), sym, prices[sym]).strip()
        )
        print(
            f" [{sym}] choke region:"
            + cell(region_series(choke, cals[sym]), sym, prices[sym]).strip()
        )

    print("\n=== 3. BROADEN INSTRUMENTS: Mideast conflict_mentions (level + delta) ===")
    print(
        "    expected: conflict -> VIX up, BNO(oil) up, UNG(gas) up, GDX(gold miners) up"
    )
    for sym in ("^VIX", "BNO", "UNG", "GDX"):
        if sym not in prices:
            continue
        s = region_series(broad, cals[sym])
        print(cell(s, sym, prices[sym], "level"))
        print(cell(s, sym, prices[sym], "delta"))

    print(
        "\n=== 4. UKRAINE THEATRE: Russia+Ukraine conflict_mentions (level + delta) ==="
    )
    print("    expected: conflict -> UNG(gas) up, USO(oil) up, WEAT(wheat) up")
    for sym in ("UNG", "USO", "WEAT"):
        if sym not in prices:
            continue
        s = region_series(ukr, cals[sym])
        print(cell(s, sym, prices[sym], "level"))
        print(cell(s, sym, prices[sym], "delta"))


if __name__ == "__main__":
    main()
