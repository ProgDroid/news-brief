# backtest/avpull/universe.py
"""Backtest universe: ~37 diversified large-caps (watchlist single-stocks +
sector spread) to break the pilot's all-trending-up confound. yfinance and AV
use the same symbol for every name here. See spec for the survivorship-bias caveat."""

UNIVERSE: list[str] = [
    # watchlist single-stocks
    "CVX",
    "MU",
    "RGLD",
    "ESLT",
    "AVAV",
    # tech
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMD",
    "CRM",
    # financials
    "JPM",
    "BAC",
    "GS",
    "V",
    # healthcare
    "JNJ",
    "UNH",
    "PFE",
    "LLY",
    # energy
    "XOM",
    "COP",
    # consumer
    "AMZN",
    "WMT",
    "KO",
    "PG",
    "MCD",
    "NKE",
    # industrials
    "CAT",
    "BA",
    "HON",
    "GE",
    "LMT",
    # comms
    "META",
    "DIS",
    "NFLX",
    # materials
    "NEM",
    "FCX",
]
