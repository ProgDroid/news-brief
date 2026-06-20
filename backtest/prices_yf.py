"""Historical daily closes for the backtest. The pure transform is unit-tested;
the live yfinance fetch is operator-run only (no network in CI)."""

from backtest.series import PriceSeries


def to_price_series(ticker: str, rows: list[tuple[str, float]]) -> PriceSeries:
    return PriceSeries(ticker=ticker, closes={d: float(c) for d, c in rows})


def fetch_price_series(yf_symbol: str, start: str, end: str) -> PriceSeries:
    import yfinance as yf  # local import: keeps the dep out of the engine/CI path

    df = yf.download(yf_symbol, start=start, end=end, auto_adjust=True, progress=False)
    rows = [(ts.strftime("%Y-%m-%d"), float(row["Close"])) for ts, row in df.iterrows()]
    return to_price_series(yf_symbol, rows)
