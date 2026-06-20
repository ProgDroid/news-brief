"""End-to-end backtest runner: pool pairs across tickers, sweep horizons, emit
metrics + a go/no-go report. Offline; fed by any SentimentSource."""

from backtest.align import align, to_delta
from backtest.evaluation import best_horizon, bonferroni_note
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
) -> dict:
    horizons_out: dict[int, dict] = {}
    for h in horizons:
        pooled: list[tuple[float, float]] = []
        for tkr, s in sentiment_by_ticker.items():
            if tkr not in prices_by_ticker:
                continue
            series = to_delta(s) if mode == "delta" else s
            fwd = forward_returns(prices_by_ticker[tkr], [h])
            pooled.extend(align(series, fwd, h))
        horizons_out[h] = {
            "ic": spearman_rank_ic(pooled),
            "quantiles": quantile_returns(pooled, q=q),
            "hit_rate": hit_rate(pooled),
            "n": len(pooled),
        }
    ic_by_h = {h: horizons_out[h]["ic"] for h in horizons}
    return {
        "horizons": horizons_out,
        "best_horizon": best_horizon(ic_by_h) if ic_by_h else None,
        "caveat": bonferroni_note(len(horizons)),
        "mode": mode,
    }


def report_markdown(result: dict) -> str:
    lines = [
        "## Bigdata.com sentiment backtest — go/no-go (https://bigdata.com)",
        f"Mode: **{result['mode']}** · best horizon (discovery): "
        f"**{result['best_horizon']}d**",
        "",
        "| Horizon (d) | n | Rank IC | Hit rate | Quantile fwd returns (low→high) |",
        "|---|---|---|---|---|",
    ]
    for h, m in sorted(result["horizons"].items()):
        qs = ", ".join(f"{x:.4f}" for x in m["quantiles"])
        lines.append(f"| {h} | {m['n']} | {m['ic']:.4f} | {m['hit_rate']:.2%} | {qs} |")
    lines += ["", f"> {result['caveat']}"]
    return "\n".join(lines)
