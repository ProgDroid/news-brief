# enrichment/providers_bigdata.py
"""Bigdata.com (RavenPack) REST client — production provider.

Live-verified against api.bigdata.com on 2026-07-19. Auth is X-API-KEY.
Every public method degrades to an error-tagged bundle instead of raising, so a
Bigdata outage can never break the brief."""

from datetime import datetime, timedelta, timezone

import requests

from common import log

from . import config
from .models import (
    EvidenceDoc,  # noqa: F401 -- parsed into ThematicBundle.docs in Task 6
    Event,  # noqa: F401 -- parsed into SymbolBundle.events in Task 5
    SentimentScore,  # noqa: F401 -- parsed into SymbolBundle.sentiment in Task 5
    SymbolBundle,
    ThematicBundle,
)

SENTIMENT_LOOKBACK_DAYS = 60
EVENTS_FORWARD_DAYS = 90
SEARCH_MAX_CHUNKS = 2


def _iso_date(ts: str | None) -> str:
    """Trim an ISO timestamp to its date; '' for None."""
    return ts[:10] if ts else ""


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _sentiment_window() -> tuple[str, str]:
    end = _today()
    start = end - timedelta(days=SENTIMENT_LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _events_window() -> tuple[str, str]:
    start = _today()
    end = start + timedelta(days=EVENTS_FORWARD_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


class BigdataProvider:
    """Production REST client for Bigdata.com (RavenPack) enrichment.

    v1 does not populate SymbolBundle.evidence (defaults to []); thematic
    evidence comes via thematic_bundle."""

    name = "bigdata"

    def __init__(self, api_key: str, base_url: str):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._entity_cache: dict[str, str | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"X-API-KEY": api_key})

    # --- HTTP helpers (one network call each; named so tests can monkeypatch) ---
    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base}{path}",
            json=payload,
            timeout=config.ENRICHMENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _find_entity(self, ticker: str) -> dict:
        return self._post("/v1/knowledge-graph/companies", {"query": ticker})

    def _get_sentiment(self, eid: str, start: str, end: str) -> dict:
        return self._post(
            "/v1/entity-sentiment/",
            {
                "identifier": {"type": "rp_entity_id", "value": eid},
                "timestamp": {"start": start, "end": end},
            },
        )

    def _get_events(self, eid: str, start: str, end: str) -> dict:
        return self._post(
            "/v1/events-calendar/query",
            {
                "rp_entity_id": [eid],
                "start_date": start,
                "end_date": end,
                "categories": ["earnings-call", "conference-call"],
                "limit": 100,
            },
        )

    def _search(self, query: str) -> dict:
        return self._post(
            "/v1/search",
            {
                "search_mode": "fast",
                "query": {
                    "text": query,
                    "filters": {},
                    "max_chunks": SEARCH_MAX_CHUNKS,
                },
            },
        )

    # --- resolution (cached per design: never re-resolve a known entity) ---
    def _resolve(self, ticker: str) -> str | None:
        if ticker in self._entity_cache:
            return self._entity_cache[ticker]
        rows = self._find_entity(ticker).get("results") or []
        eid = rows[0].get("id") if rows else None
        self._entity_cache[ticker] = eid
        return eid

    # --- public Provider interface ---
    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(ticker, None, None, error="no entity match")
            return SymbolBundle(ticker, eid, None)  # full parse in Task 5
        except Exception as e:  # degrade, never crash the brief
            log.warning("Bigdata symbol_bundle(%s) degraded: %s", ticker, e)
            return SymbolBundle(ticker, None, None, error=str(e))

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            self._search(theme)
            return ThematicBundle(theme=theme)  # full parse in Task 6
        except Exception as e:
            log.warning("Bigdata thematic_bundle(%s) degraded: %s", theme, e)
            return ThematicBundle(theme=theme, error=str(e))
