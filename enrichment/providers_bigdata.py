# enrichment/providers_bigdata.py
"""Bigdata.com (RavenPack) REST client — production provider.

Flag-gated OFF in production until business-email REST creds land; unit-tested
against documented-shape fixtures. Every public method degrades to an
error-tagged bundle instead of raising, so a Bigdata outage can never break the
brief. JSON paths below must match docs.bigdata.com (confirm via Context7)."""

import requests

from common import log

from . import config
from .models import (
    EvidenceDoc,
    Event,
    SentimentScore,
    SymbolBundle,
    ThematicBundle,
)


def _iso_date(ts: str | None) -> str:
    """Trim an ISO timestamp to its date; pass through a bare date; '' for None."""
    if not ts:
        return ""
    return ts[:10]


class BigdataProvider:
    name = "bigdata"

    def __init__(self, api_key: str, base_url: str):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._entity_cache: dict[str, str | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

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
        return self._post("/securities/search", {"query": ticker})

    def _get_sentiment(self, rp_entity_id: str) -> dict:
        return self._post("/sentiment/tearsheet", {"rp_entity_id": rp_entity_id})

    def _get_events(self, rp_entity_id: str) -> dict:
        return self._post(
            "/events/calendar",
            {
                "rp_entity_id": rp_entity_id,
                "categories": ["earnings-call", "conference-call"],
            },
        )

    def _search(self, query: str) -> dict:
        return self._post("/search", {"query": query, "mode": "smart"})

    # --- resolution (cached per design: never re-resolve a known entity) ---
    def _resolve(self, ticker: str) -> str | None:
        if ticker in self._entity_cache:
            return self._entity_cache[ticker]
        data = self._find_entity(ticker)
        results = data.get("results") or []
        eid = results[0].get("rp_entity_id") if results else None
        self._entity_cache[ticker] = eid
        return eid

    # --- public Provider interface ---
    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(
                    ticker=ticker,
                    rp_entity_id=None,
                    sentiment=None,
                    error="no entity match",
                )
            s = self._get_sentiment(eid)
            sentiment = SentimentScore(
                current=s.get("current"),
                baseline=s.get("baseline"),
                zscore_1mo=s.get("zscore_1mo"),
                zscore_1qt=s.get("zscore_1qt"),
                regime=s.get("regime", "Unknown"),
                confidence=s.get("confidence"),
            )
            events = [
                Event(
                    category=e.get("category", ""),
                    title=e.get("headline", ""),
                    date=_iso_date(e.get("date")),
                    url=e.get("url"),
                )
                for e in self._get_events(eid).get("events", [])
            ]
            return SymbolBundle(
                ticker=ticker, rp_entity_id=eid, sentiment=sentiment, events=events
            )
        except Exception as e:  # degrade, never crash the brief
            log.warning("Bigdata symbol_bundle(%s) degraded: %s", ticker, e)
            return SymbolBundle(
                ticker=ticker, rp_entity_id=None, sentiment=None, error=str(e)
            )

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            docs = [
                EvidenceDoc(
                    headline=d.get("headline", ""),
                    source=d.get("source_name", ""),
                    date=_iso_date(d.get("timestamp")),
                    url=d.get("url"),
                    sentiment=d.get("sentiment"),
                )
                for d in self._search(theme).get("documents", [])
            ]
            return ThematicBundle(theme=theme, docs=docs)
        except Exception as e:
            log.warning("Bigdata thematic_bundle(%s) degraded: %s", theme, e)
            return ThematicBundle(theme=theme, error=str(e))
