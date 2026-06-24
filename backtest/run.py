"""End-to-end backtest runner: pool pairs across tickers, sweep horizons, select
the horizon on a discovery split and CONFIRM it on a held-out (later) window, then
emit metrics + a report. Offline; fed by any SentimentSource."""

from backtest.align import align_dated, to_delta, to_zscore
from backtest.evaluation import best_horizon, bonferroni_note, split_pairs
from backtest.metrics import hit_rate, quantile_returns, spearman_rank_ic
from backtest.returns import forward_returns
from backtest.series import PriceSeries, SentimentSeries


def run_backtest(
    sentiment_by_ticker: dict[str, SentimentSeries],
    prices_by_ticker: dict[str, PriceSeries],
    horizons: list[int],
    *,
    mode: str = "level",
    q: int = 5,
    split_frac: float = 0.5,
    standardize: bool = False,
) -> dict:
    """Discovery/confirmation discipline:
    1. Pool (date, sentiment, fwd_return) across tickers per horizon, sort by date.
    2. Split TEMPORALLY: earlier `split_frac` = discovery, rest = held-out.
    3. Select best_horizon by arg-max |IC| on DISCOVERY only.
    4. Report the full metric suite on the HELD-OUT set (the confirmation).
    The discovery IC table is in-sample/selection-biased; only the held-out
    confirmation is out-of-sample.

    `standardize=True` z-scores sentiment WITHIN each ticker before pooling, so
    cross-ticker level offsets don't contaminate the cross-sectional rank IC.
    """
    discovery_ic: dict[int, float] = {}
    holdout: dict[int, dict] = {}
    counts: dict[int, dict] = {}
    for h in horizons:
        pooled: list[tuple[str, float, float]] = []
        for tkr, s in sentiment_by_ticker.items():
            if tkr not in prices_by_ticker:
                continue
            series = to_delta(s) if mode == "delta" else s
            if standardize:
                series = to_zscore(series)
            fwd = forward_returns(prices_by_ticker[tkr], [h])
            pooled.extend(align_dated(series, fwd, h))
        pooled.sort(key=lambda row: row[0])  # temporal order for an honest split
        disc_rows, hold_rows = split_pairs(pooled, split_frac)
        disc = [(sv, r) for _, sv, r in disc_rows]
        hold = [(sv, r) for _, sv, r in hold_rows]
        discovery_ic[h] = spearman_rank_ic(disc)
        holdout[h] = {
            "ic": spearman_rank_ic(hold),
            "quantiles": quantile_returns(hold, q=q),
            "hit_rate": hit_rate(hold),
            "n": len(hold),
        }
        counts[h] = {
            "n_total": len(pooled),
            "n_discovery": len(disc),
            "n_holdout": len(hold),
        }
    best = best_horizon(discovery_ic) if discovery_ic else None
    return {
        "mode": mode,
        "standardize": standardize,
        "split_frac": split_frac,
        "discovery_ic": discovery_ic,
        "holdout": holdout,
        "best_horizon": best,
        "confirmation": holdout.get(best) if best is not None else None,
        "counts": counts,
        "caveat": bonferroni_note(len(horizons)),
    }


_DISCOVERY_CAVEAT = (
    "⚠ The discovery sweep below is **IN-SAMPLE and selection-biased** (best horizon "
    "= arg-max |IC| over the swept horizons). Only the **held-out confirmation** row "
    "is out-of-sample. Treat the discovery table as exploratory; base any sizing "
    "decision on the held-out confirmation, not the discovery IC."
)


def report_markdown(
    result: dict,
    *,
    source_label: str = "Bigdata.com",
    source_url: str = "https://bigdata.com",
) -> str:
    best = result["best_horizon"]
    frac = result["split_frac"]
    sentiment_scale = (
        "per-ticker standardized" if result.get("standardize") else "raw levels"
    )
    lines = [
        f"## {source_label} sentiment backtest — discovery + held-out "
        f"confirmation ({source_url})",
        f"> {_DISCOVERY_CAVEAT}",
        "",
        f"Mode: **{result['mode']}** · sentiment: **{sentiment_scale}** · "
        f"split: {frac:.0%} discovery / "
        f"{1 - frac:.0%} held-out (temporal: earlier→discovery, later→held-out)",
        "",
        "### Discovery — in-sample horizon selection (selection-biased)",
        "| Horizon (d) | n (discovery) | Rank IC (in-sample) |",
        "|---|---|---|",
    ]
    for h in sorted(result["discovery_ic"]):
        nd = result["counts"][h]["n_discovery"]
        lines.append(f"| {h} | {nd} | {result['discovery_ic'][h]:.4f} |")
    lines += [
        "",
        (
            f"Selected horizon (arg-max |IC| on discovery): **{best}d**"
            if best is not None
            else "Selected horizon: **n/a** (no data)"
        ),
        "",
        "### Held-out confirmation — out-of-sample, at the selected horizon",
    ]
    conf = result["confirmation"]
    if conf and conf["n"] > 0:
        qs = ", ".join(f"{x:.4f}" for x in conf["quantiles"])
        lines += [
            "| Horizon (d) | n (held-out) | Rank IC | Hit rate | "
            "Quantile fwd returns (low→high) |",
            "|---|---|---|---|---|",
            f"| {best} | {conf['n']} | {conf['ic']:.4f} | {conf['hit_rate']:.2%} | {qs} |",
        ]
    else:
        lines.append("_Insufficient held-out data to confirm — widen the sample._")
    lines += ["", f"> {result['caveat']}"]
    return "\n".join(lines)
