"""Historical daily closes for the backtest. The pure transform is unit-tested;
the live yfinance fetch is operator-run only (no network in CI)."""

from backtest.series import PriceSeries


def to_price_series(ticker: str, rows: list[tuple[str, float]]) -> PriceSeries:
    return PriceSeries(ticker=ticker, closes={d: float(c) for d, c in rows})


def _closes_from_frame(df) -> list[tuple[str, float]]:
    """Extract (date, close) rows from a yfinance frame, tolerating both flat
    columns and the single-symbol MultiIndex shape. yfinance returns columns
    like ('Close', <SYM>) even for one symbol, so df['Close'] is then a 1-col
    DataFrame rather than a scalar Series — take its first column in that case."""
    close = df["Close"]
    if hasattr(close, "columns"):  # MultiIndex single-symbol -> sub-DataFrame
        close = close.iloc[:, 0]
    return [(ts.strftime("%Y-%m-%d"), float(v)) for ts, v in close.items()]


def fetch_price_series(yf_symbol: str, start: str, end: str) -> PriceSeries:
    import yfinance as yf  # local import: keeps the dep out of the engine/CI path

    df = yf.download(yf_symbol, start=start, end=end, auto_adjust=True, progress=False)
    return to_price_series(yf_symbol, _closes_from_frame(df))
