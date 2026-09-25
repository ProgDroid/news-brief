"""common.is_quote_page: Reuters instrument pages that reached us as "news".

Every title below is REAL, from live Google News fetches on 2026-09-25 (spec
2026-09-25 section 2.2). The negatives are real headlines from the same feeds,
including a markets column, because the failure that matters is dropping news.
"""

import pytest

import common

QUOTE_PAGES = [
    "MSTS.DE - Reuters",
    "BESG.TO - Reuters",
    "XEQ2.DE - Reuters",
    "QQQB.OQ - Reuters",
    "8PB.MU - Reuters",
    "SOGN.HA - | Stock Price & Latest News - Reuters",
    "(UN) | Stock Price & Latest News - Reuters",
    # How the title can sit in the database: capture stores feedparser's value,
    # and some paths carry the entity escaped.
    "IBXVF.PK - | Stock Price &amp; Latest News - Reuters",
]

NEWS = [
    "Mapping the Market: Why 3M shares might be set to rally again - Reuters",
    "Sterling treads water at 3-month low after weekly drop on dollar rally - Reuters",
    "US-sanctioned oil tanker Sibu 1 rescued from Somali pirates - Reuters",
    "Pact with Saudi and Pakistan could expand to Muslim world, Iran, Turkish speaker says - Reuters",
    "Iran signals a ceasefire",
    "",
]


@pytest.mark.parametrize("title", QUOTE_PAGES)
def test_a_quote_page_is_recognised(title):
    assert common.is_quote_page(title) is True


@pytest.mark.parametrize("title", NEWS)
def test_real_news_is_never_a_quote_page(title):
    assert common.is_quote_page(title) is False


def test_none_is_not_a_quote_page():
    assert common.is_quote_page(None) is False
