"""Crypto seam: Kraken pair resolution + pricing + asset-class dispatch."""

import trading


# ── resolve_kraken_pair ───────────────────────────────────────────────────────
def test_btc_maps_to_xbt_usd_pair():
    assert trading.resolve_kraken_pair("BTC", {}) == "XBTUSD"


def test_eth_maps_to_eth_usd_pair():
    assert trading.resolve_kraken_pair("ETH", {}) == "ETHUSD"


def test_resolve_kraken_is_case_insensitive():
    assert trading.resolve_kraken_pair("btc", {}) == "XBTUSD"


def test_crypto_override_is_authoritative():
    assert trading.resolve_kraken_pair("FOO", {"FOO": "FOOUSD"}) == "FOOUSD"


def test_unknown_coin_returns_none():
    assert trading.resolve_kraken_pair("NOTACOIN", {}) is None
