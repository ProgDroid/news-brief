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
    EvidenceDoc,
    Event,
    SentimentScore,
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


def _pick_entity(rows: list[dict], ticker: str) -> str | None:
    """Choose the entity id for an exact ticker match, preferring PUBLIC."""
    tkr = ticker.upper()
    matches = [r for r in rows if (r.get("ticker") or "").upper() == tkr]
    if not matches:
        return None
    public = [r for r in matches if r.get("type") == "PUBLIC"]
    chosen = public[0] if public else matches[0]
    return chosen.get("id")


def _score_from_values(values: list[dict]) -> SentimentScore | None:
    if not values:
        return None
    pts = sorted(values, key=lambda v: v.get("date", ""))
    latest = pts[-1]
    daily = [v["daily_sentiment"] for v in pts if v.get("daily_sentiment") is not None]
    mean = sum(daily) / len(daily) if daily else None
    latest_ds = latest.get("daily_sentiment")
    delta = (latest_ds - mean) if (mean is not None and latest_ds is not None) else None
    return SentimentScore(
        as_of=latest.get("date"),
        daily_sentiment=latest_ds,
        sentiment_pressure=latest.get("sentiment_pressure"),
        abnormal_media_attention=latest.get("abnormal_media_attention"),
        trend_mean=mean,
        trend_delta=delta,
        n_points=len(pts),
    )


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
        self._usage = 0

    # --- HTTP helpers (one network call each; named so tests can monkeypatch) ---
    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base}{path}",
            json=payload,
            timeout=config.ENRICHMENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or (data.get("metadata") or {}).get("usage") or {}
        self._usage += sum(v for v in usage.values() if isinstance(v, (int, float)))
        return data

    @property
    def usage_units(self) -> float:
        return self._usage

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
        eid = _pick_entity(rows, ticker)
        self._entity_cache[ticker] = eid
        return eid

    # --- public Provider interface ---
    def symbol_bundle(self, ticker: str) -> SymbolBundle:
        try:
            eid = self._resolve(ticker)
            if not eid:
                return SymbolBundle(ticker, None, None, error="no entity match")
            sent_start, sent_end = _sentiment_window()
            values = (
                self._get_sentiment(eid, sent_start, sent_end).get("results") or [{}]
            )[0].get("values") or []
            sentiment = _score_from_values(values)
            ev_start, ev_end = _events_window()
            ev_rows = (
                self._get_events(eid, ev_start, ev_end).get("results", {}).get(eid, [])
            )
            events = [
                Event(
                    category=e.get("category", ""),
                    title=e.get("title", ""),
                    date=_iso_date(e.get("event_datetime")),
                    url=None,
                )
                for e in ev_rows
            ]
            return SymbolBundle(ticker, eid, sentiment, events=events)
        except Exception as e:  # degrade, never crash the brief
            log.warning("Bigdata symbol_bundle(%s) degraded: %s", ticker, e)
            return SymbolBundle(ticker, None, None, error=str(e))

    def thematic_bundle(self, theme: str) -> ThematicBundle:
        try:
            results = self._search(theme).get("results") or []
            docs = [
                EvidenceDoc(
                    headline=r.get("headline", ""),
                    source=(r.get("source") or {}).get("name", ""),
                    date=_iso_date(r.get("timestamp")),
                    url=r.get("url"),
                    sentiment=(r.get("chunks") or [{}])[0].get("sentiment"),
                )
                for r in results
            ]
            return ThematicBundle(theme=theme, docs=docs)
        except Exception as e:
            log.warning("Bigdata thematic_bundle(%s) degraded: %s", theme, e)
            return ThematicBundle(theme=theme, error=str(e))
