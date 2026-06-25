# backtest/gdelt/run_gdelt_spike.py
"""Operator-run GDELT signal-validation spike. Pulls GDELT 1.0 daily events over
a window, builds a Middle-East conflict signal, and measures its IC / hit-rate
against oil/gold/equity forward returns via the existing backtest engine. NOT
imported by the cron app or CI. Network + yfinance required.

Run:
  python -m backtest.gdelt.run_gdelt_spike \\
      --start 2025-09-15 --end 2026-06-20 \\
      --cache .gdelt_cache --out backtest/gdelt/out
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backtest.gdelt.aggregate import (
    MIDEAST_FIPS,
    GdeltDaily,
    fold_daily,
    merge_daily,
)
from backtest.gdelt.events import parse_row
from backtest.gdelt.fetch import fetch_day_rows
from backtest.gdelt.snap import snap_forward, to_sentiment_series
from backtest.prices_yf import fetch_price_series
from backtest.run import report_markdown, run_backtest

HORIZONS = [1, 3, 5, 10]
# (yfinance symbol, expected IC sign for the PRIMARY conflict_mentions signal)
INSTRUMENTS = [("USO", +1), ("GLD", +1), ("SPY", -1), ("ITA", +1)]
PRIMARY_FIELD = "conflict_mentions"
FIELDS = ["conflict_mentions", "mean_tone", "mean_goldstein"]
MODES = ["level", "delta"]
_IC_FLOOR = 0.03  # |held-out IC| below this is treated as noise for the sign check


def daterange(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [
        (d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range((d1 - d0).days + 1)
    ]


def build_dailies(start: str, end: str, cache: str | None) -> dict[str, GdeltDaily]:
    """Pull every day in [start, end] and fold into calendar-day accumulators."""
    acc: dict[str, GdeltDaily] = {}
    for ymd in daterange(start, end):
        events = [
            ev for ev in map(parse_row, fetch_day_rows(ymd, cache_dir=cache)) if ev
        ]
        for d, rec in fold_daily(events, MIDEAST_FIPS).items():
            acc[d] = merge_daily(acc[d], rec, date_iso=d) if d in acc else rec
    return acc


def _sign_ok(r: dict) -> str:
    conf = r["result"]["confirmation"]
    if not conf or r["field"] != PRIMARY_FIELD or r["mode"] != "level":
        return "—"  # em dash: not a pre-registered decision cell
    ic = conf["ic"]
    correct = (ic > 0) == (r["expected_sign"] > 0)
    return "yes" if correct and abs(ic) > _IC_FLOOR else "no"


def summarize(results: list[dict]) -> str:
    lines = [
        "| instrument | field | mode | best h | held-out IC | hit rate | n | sign ok |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        conf = r["result"]["confirmation"]
        ic = f"{conf['ic']:.4f}" if conf else "n/a"
        hr = f"{conf['hit_rate']:.2%}" if conf else "n/a"
        n = conf["n"] if conf else 0
        lines.append(
            f"| {r['symbol']} | {r['field']} | {r['mode']} "
            f"| {r['result']['best_horizon']} | {ic} | {hr} | {n} | {_sign_ok(r)} |"
        )
    return "\n".join(lines)


def main(start: str, end: str, cache: str | None, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dailies = build_dailies(start, end, cache)
    print(f"GDELT calendar days with region events: {len(dailies)}")

    results: list[dict] = []
    for sym, sign in INSTRUMENTS:
        try:
            prices = fetch_price_series(sym, start, end)
        except Exception as e:  # one flaky symbol must not kill the run
            print(f"price fetch failed for {sym}: {e}; skipping")
            continue
        snapped = snap_forward(dailies, set(prices.closes.keys()))
        for field in FIELDS:
            series = to_sentiment_series(snapped, field, label=f"{sym}:{field}")
            for mode in MODES:
                res = run_backtest(
                    {sym: series}, {sym: prices}, HORIZONS, mode=mode, standardize=True
                )
                (out / f"RESULT-{sym}-{field}-{mode}.md").write_text(
                    report_markdown(
                        res,
                        source_label=f"GDELT MidEast conflict ({field}/{mode}) vs {sym}",
                        source_url="https://www.gdeltproject.org",
                    )
                )
                results.append(
                    {
                        "symbol": sym,
                        "field": field,
                        "mode": mode,
                        "expected_sign": sign,
                        "result": res,
                    }
                )

    grid = summarize(results)
    (out / "SUMMARY.md").write_text(grid)
    (out / "SUMMARY.json").write_text(
        json.dumps(
            [
                {k: r[k] for k in ("symbol", "field", "mode", "expected_sign")}
                | {
                    "confirmation": r["result"]["confirmation"],
                    "best_horizon": r["result"]["best_horizon"],
                }
                for r in results
            ],
            indent=2,
        )
    )
    print(grid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--cache", default=".gdelt_cache")
    ap.add_argument("--out", default="backtest/gdelt/out")
    a = ap.parse_args()
    main(a.start, a.end, a.cache, a.out)
