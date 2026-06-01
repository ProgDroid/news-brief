#!/usr/bin/env python3
"""
newsbrief - Daily geopolitical/macro briefing via Claude Batch API

Modes:
  submit   — fetch feeds, query Chroma, submit batch job (~8pm UTC)
  collect  — poll for results, deliver via Telegram, save signals, open paper positions (~6am UTC)
  weekly   — weekly summary from last 7 briefs + mark the paper book to market (Sunday ~9pm UTC)
  commands — process pending Telegram commands without submitting
  run      — submit + collect synchronously (for testing)
  paper    — open paper positions from today's signals (also run inside collect)

The image entrypoint is `python brief.py`, so the MODE is the command argument. The
committed docker-compose.yml defines one service per mode from a single shared anchor;
schedule them with your container scheduler or host cron:
  0 20 * * *   docker compose run --rm newsbrief-submit
  0  6 * * *   docker compose run --rm newsbrief-collect
  0 21 * * 0   docker compose run --rm newsbrief-weekly
  */30 * * * * docker compose run --rm newsbrief-commands
"""

import base64
import os
import re
import json
import time
import logging
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/logs/newsbrief.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Chroma MCP HTTP endpoint
# NOTE: This endpoint is called via HTTP POST with JSON-RPC 2.0 format.
# If you are running the MCP server locally or via a different transport,
# update CHROMA_MCP_URL in your .env accordingly.
CHROMA_MCP_URL = os.environ.get(
    "CHROMA_MCP_URL", "https://progdroid--podcast-mcp-server-mcp-server.modal.run/mcp"
)

ANTHROPIC_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 16384  # whole-turn budget; web-search loop + brief + signals JSON

STATE_FILE = Path("/app/logs/batch_state.json")
FEEDBACK_FILE = Path("/app/logs/feedback.json")
BRIEFS_DIR = Path("/app/logs/briefs")
WEEKLY_DIR = Path("/app/logs/weekly")

# ── Trading212 portfolio ──────────────────────────────────────────────────────
# Read-only API key. Base URL: live for real account, demo for practice.
# .strip() guards against a trailing newline/whitespace leaking in from .env or a
# Docker secret — T212 rejects a malformed Authorization value with 401.
T212_API_KEY_ID = os.environ.get("T212_API_KEY_ID", "").strip()
T212_API_KEY = os.environ.get("T212_API_KEY", "").strip()
T212_BASE_URL = os.environ.get("T212_BASE_URL", "https://live.trading212.com").strip()


def t212_auth_header() -> str:
    """Build the T212 HTTP Basic Authorization value from the configured credentials.

    base64.b64encode never inserts line breaks (unlike GNU `base64` without -w0, or
    the legacy base64.encodebytes), so the encoded value is always header-safe.
    """
    token = base64.b64encode(
        f"{T212_API_KEY_ID}:{T212_API_KEY}".encode("utf-8")
    ).decode("utf-8")
    return f"Basic {token}"


# Self-hosted Nitter (Twitter/X mirror) reachable on the container's Docker network.
# Default targets a service named `nitter` on Nitter's default internal port 8080;
# override NITTER_BASE_URL if your instance listens on a different host/port.
NITTER_BASE_URL = (
    os.environ.get("NITTER_BASE_URL", "http://nitter:8080").strip().rstrip("/")
)

THESIS_FILE = Path("/app/logs/theses.json")

SIGNALS_DIR = Path("/app/logs/signals")

PAPER_DIR = Path("/app/logs/paper")
PAPER_BOOK_FILE = PAPER_DIR / "paper-book.json"
TICKER_MAP_FILE = PAPER_DIR / "ticker_map.json"
INSTRUMENTS_CACHE_FILE = PAPER_DIR / "instruments-cache.json"

PAPER_HORIZONS = {"1w": 7, "2w": 14, "4w": 28}  # days from entry_date
PAPER_CLOSE_HORIZON = "4w"  # close the position once this checkpoint is recorded

# ── Feed sources ──────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {
        "name": "Reuters Markets",
        # Reuters discontinued public RSS (June 2020); proxy via Google News.
        # `site:` is stable (unlike allinurl:); `when:2d` is a freshness guardrail
        # so a quiet section never feeds the LLM stale headlines as news. Only the
        # 5 newest items are used (fetch_rss max_items), so the window isn't a
        # volume control — markets still returns 100 items inside 2d.
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fmarkets&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "macro",
    },
    {
        "name": "Reuters World",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fworld&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "geo",
    },
    {
        "name": "Sinica Podcast",
        "url": "https://sinica.substack.com/feed",
        "category": "china",
    },
    {
        "name": "Un-Diplomatic",
        "url": "https://www.un-diplomatic.com/feed",
        "category": "geo",
    },
    {
        "name": "Observing Japan",
        "url": "https://observingjapan.substack.com/feed",
        "category": "japan",
    },
    {
        "name": "Pinecone Weekly Brief",
        "url": "https://pineconemacroresearch.substack.com/feed",
        "category": "geo",
    },
    {
        "name": "Intersubjectively Transmissible",
        "url": "https://jashap.substack.com/feed",
        "category": "macro",
    },
    {
        "name": "Marko Papic (@geo_papic)",
        # X killed unauthenticated scraping and rsshub.app's public route is dead;
        # served via the self-hosted Nitter on the container's Docker network.
        "url": f"{NITTER_BASE_URL}/geo_papic/rss",
        "category": "geo",
    },
    {
        "name": "Jacob Shapiro (@jacobshap)",
        "url": f"{NITTER_BASE_URL}/jacobshap/rss",
        "category": "geo",
    },
]

WEB_SOURCES = [
    {
        "name": "BCA Research — Iran Conflict Daily Dashboard",
        "url": "https://www.bcaresearch.com/collection/bcas-iran-conflict-daily-dashboard",
        "category": "iran",
    },
]

# Each topic drives both a web search query and a Chroma query
TOPICS = [
    {
        "label": "ukraine",
        "search": "Ukraine war latest news",
        "chroma": "Ukraine war Russia ceasefire frontline",
        "recency": True,
    },
    {
        "label": "iran",
        "search": "US Iran negotiations Hormuz",
        "chroma": "Iran Hormuz oil shipping blockade",
        "recency": True,
    },
    {
        "label": "korea",
        "search": "North Korea South Korea news",
        "chroma": "Korea peninsula Kim Jong Un South Korea",
        "recency": False,
    },
    {
        "label": "japan",
        "search": "Japan geopolitics economy BOJ",
        "chroma": "Japan yen BOJ monetary policy",
        "recency": False,
    },
    {
        "label": "china",
        "search": "China Taiwan economy trade",
        "chroma": "China Taiwan trade economy Xi Jinping",
        "recency": False,
    },
]


# ── Feedback / memory ─────────────────────────────────────────────────────────
def load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        return json.loads(FEEDBACK_FILE.read_text())
    return {"focus": [], "mute": [], "notes": []}


def save_feedback(fb: dict):
    FEEDBACK_FILE.write_text(json.dumps(fb, indent=2))


def feedback_summary(fb: dict) -> str:
    lines = []
    if fb.get("focus"):
        lines.append("Focus: " + ", ".join(fb["focus"]))
    if fb.get("mute"):
        lines.append("Muted: " + ", ".join(fb["mute"]))
    if fb.get("notes"):
        lines.append("Notes:\n" + "\n".join(f"  • {n}" for n in fb["notes"]))
    return "\n".join(lines) if lines else "No active overrides."


# ── Telegram ──────────────────────────────────────────────────────────────────
HELP_TEXT = """<b>newsbrief commands</b>

/focus [topic or phrase]
  Emphasise something in upcoming briefs.
  e.g. <code>/focus ceasefire talks Ukraine</code>

/mute [topic]
  Reduce a quiet section to one line.
  e.g. <code>/mute korea</code>

/note [free text]
  Inject a one-off instruction into the next brief.
  e.g. <code>/note watch JPY moves above 155</code>

/close [TICKER]
  Close an open paper position early at the current mark.
  e.g. <code>/close AAPL_US_EQ</code>

/reset — clear all overrides
/status — show current overrides
/help — this message
"""

TELEGRAM_MAX_LEN = 4000
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}


def telegram_send(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.error(f"Telegram {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


def telegram_get_updates(offset: int = 0) -> list:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        return resp.json().get("result", []) if resp.ok else []
    except Exception:
        return []


def process_telegram_commands():
    """Poll for bot messages and apply feedback commands."""
    state = load_state() or {}
    offset = state.get("tg_offset", 0)
    updates = telegram_get_updates(offset)
    if not updates:
        return

    fb = load_feedback()
    new_offset = offset

    for update in updates:
        new_offset = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        if text.startswith("/focus "):
            item = text[7:].strip()
            if item and item not in fb["focus"]:
                fb["focus"].append(item)
            telegram_send(f"✅ Focus added: <b>{item}</b>\n\n{feedback_summary(fb)}")

        elif text.startswith("/mute "):
            item = text[6:].strip().lower()
            if item and item not in fb["mute"]:
                fb["mute"].append(item)
            telegram_send(f"🔇 Muted: <b>{item}</b>\n\n{feedback_summary(fb)}")

        elif text.startswith("/note "):
            note = text[6:].strip()
            if note:
                fb["notes"].append(note)
            telegram_send(f"📝 Note added: <i>{note}</i>\n\n{feedback_summary(fb)}")

        elif text == "/reset":
            fb = {"focus": [], "mute": [], "notes": []}
            telegram_send("🔄 All overrides cleared.")

        elif text == "/status":
            telegram_send(f"<b>Current overrides</b>\n\n{feedback_summary(fb)}")

        elif text in ("/help", "/start"):
            telegram_send(HELP_TEXT)

        elif text.startswith("/thesis "):
            # /thesis SHEL = long oil supply tightness
            # /thesis cluster:energy = long structural oil demand
            body = text[8:].strip()
            if "=" in body:
                key, val = body.split("=", 1)
                theses = load_theses()
                theses[key.strip()] = val.strip()
                save_theses(theses)
                telegram_send(f"📌 Thesis set: <b>{key.strip()}</b> — {val.strip()}")
            else:
                telegram_send("Format: <code>/thesis TICKER = your thesis</code>")

        elif text.startswith("/dig "):
            query = text[5:].strip()
            since = None
            m = re.match(r"since:(\d{4}-\d{2}-\d{2})\s+(.*)", query)
            if m:
                since, query = m.group(1), m.group(2)
            telegram_send(f"🔎 Digging into: <i>{query}</i>…")
            answer = run_dig(query, since=since)
            for chunk in split_html_message(sanitise_html(answer)):
                telegram_send(chunk)
                time.sleep(0.4)

        elif text.startswith("/close "):
            tkr = text[7:].strip()
            book = load_paper_book()
            matches = [
                p
                for p in book["positions"]
                if p["status"] == "open" and p["ticker"] == tkr
            ]
            if not matches:
                telegram_send(f"No open paper position for <b>{tkr}</b>.")
            else:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                closed_n = sum(
                    _close_position_at_market(p, day, "manual") for p in matches
                )
                if closed_n:
                    save_paper_book(book)
                    telegram_send(
                        f"✅ Closed {closed_n} paper position(s) for <b>{tkr}</b> (manual)."
                    )
                else:
                    telegram_send(f"⚠️ Couldn't price {tkr} — left open.")

        else:
            telegram_send("Unknown command — send /help for options.")

    save_feedback(fb)
    state["tg_offset"] = new_offset
    STATE_FILE.write_text(json.dumps(state))


def run_dig(query: str, since: str | None = None) -> str:
    """
    Deep-dive on a specific topic. Synchronous Messages API call (not batch)
    with web search, so the reader gets a fast, detailed answer in-chat.
    Pulls the most RECENT podcast commentary on the topic for current framing.
    When `since` (YYYY-MM-DD) is given, the podcast search is date-bounded to it.
    """
    chroma_excerpts = query_chroma_latest(query, n_results=4, after_date=since)
    chroma_block = (
        "\n\n".join(e[:600] for e in chroma_excerpts) or "(no recent podcast context)"
    )
    since_note = f" Focus on developments since {since}." if since else ""

    payload = {
        "model": MODEL,
        "max_tokens": 2048,
        "system": (
            "You are a geopolitical/macro analyst answering a focused follow-up "
            "question for an informed investor. Be specific and detailed — this is a "
            "drill-down, so depth is wanted, unlike the daily brief. Prioritise Reuters. "
            "The podcast context provided is the most recent commentary available; weigh "
            "it by its date and note if a view may be stale. "
            "Output Telegram HTML only: <b>, <i>, <code>, <a href>. Bullets with •. "
            "No markdown, no # headers, no code fences. Under 500 words."
        ),
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Question: {query}\n\n"
                    f"Most recent podcast analyst commentary on this topic:\n{chroma_block}\n\n"
                    f"Search the web for the latest specifics and answer in depth.{since_note}"
                ),
            }
        ],
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        return (
            "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            or "No answer returned."
        )
    except Exception as e:
        log.error(f"Dig failed: {e}")
        return f"⚠️ Dig failed: {e}"


# ── Feed fetching ─────────────────────────────────────────────────────────────
def fetch_rss(feed: dict, max_items: int = 5) -> str:
    try:
        parsed = feedparser.parse(feed["url"])
        if not parsed.entries:
            # feedparser never raises on HTTP errors — it returns empty .entries
            # and stashes the real cause in .status / .bozo_exception. Surface it
            # so a dead feed (404/000) is distinguishable from a genuinely empty one.
            status = getattr(parsed, "status", None)
            bozo_exc = getattr(parsed, "bozo_exception", None)
            detail = f"HTTP {status}" if status else (str(bozo_exc) or "unknown")
            log.warning(f"No entries: {feed['name']} ({detail})")
            return ""
        lines = [f"\n### {feed['name']} ({feed['category'].upper()})"]
        for entry in parsed.entries[:max_items]:
            title = entry.get("title", "").strip()
            summary = re.sub(
                r"<[^>]+>",
                "",
                entry.get("summary", entry.get("description", "")).strip(),
            )[:400]
            pub = entry.get("published", "")
            lines.append(f"- {title} ({pub})\n  {summary}")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"RSS failed {feed['name']}: {e}")
        return ""


def fetch_web_source(source: dict) -> str:
    try:
        resp = requests.get(
            source["url"],
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; newsbrief/1.0)"},
        )
        resp.raise_for_status()
        meta = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            resp.text,
            re.I,
        ) or re.search(
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
            resp.text,
            re.I,
        )
        content = meta.group(1).strip() if meta else resp.text[:800]
        return f"\n### {source['name']} ({source['category'].upper()})\n{content}"
    except Exception as e:
        log.warning(f"Web fetch failed {source['name']}: {e}")
        return ""


# ── Chroma DB ─────────────────────────────────────────────────────────────────
def query_chroma(query: str, n_results: int = 2) -> list[str]:
    """
    Query the podcast Chroma MCP server via HTTP.
    The server speaks JSON-RPC 2.0 over MCP's Streamable HTTP transport, which
    requires the client to advertise it accepts both application/json and
    text/event-stream; omitting that Accept header yields 406 Not Acceptable.
    Returns a list of plain-text excerpt strings.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_podcasts",
            "arguments": {"query": query, "n_results": n_results},
        },
    }
    try:
        resp = requests.post(
            CHROMA_MCP_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            # Modal serverless cold-starts can exceed 20s on the first call of a run.
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json().get("result", {}).get("content", [])
        return [b["text"] for b in content if b.get("type") == "text"]
    except Exception as e:
        log.warning(f"Chroma failed '{query}': {e}")
        return []


def query_chroma_latest(
    topic: str, n_results: int = 4, after_date: str | None = None
) -> list[str]:
    args = {"topic": topic, "n_results": n_results}
    # Note: latest_on_topic does not take after_date; use search_podcasts when date-bounding
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_podcasts" if after_date else "latest_on_topic",
            "arguments": (
                {"query": topic, "n_results": n_results, "after_date": after_date}
                if after_date
                else args
            ),
        },
    }
    try:
        resp = requests.post(
            CHROMA_MCP_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            # Modal serverless cold-starts can exceed 20s on the first call of a run.
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json().get("result", {}).get("content", [])
        return [b["text"] for b in content if b.get("type") == "text"]
    except Exception as e:
        log.warning(f"Chroma query failed '{topic}': {e}")
        return []


def build_chroma_context(fb: dict) -> str:
    """Query Chroma for each non-muted topic and format as analyst context.
    Volatile topics (recency=True) use latest_on_topic; others use semantic search."""
    muted = {m.lower() for m in fb.get("mute", [])}
    focused = {f.lower() for f in fb.get("focus", [])}
    sections = []

    for topic in TOPICS:
        label = topic["label"]
        if label in muted:
            continue
        n = 3 if any(label in f for f in focused) else 2
        if topic.get("recency"):
            excerpts = query_chroma_latest(topic["chroma"], n_results=n)
        else:
            excerpts = query_chroma(topic["chroma"], n_results=n)
        if not excerpts:
            continue
        lines = [f"\n#### Podcast context — {label.upper()}"]
        for excerpt in excerpts:
            lines.append(excerpt[:500])
        sections.append("\n".join(lines))

    return "\n".join(sections) if sections else "(no podcast context retrieved)"


# ── Trading212 portfolio ──────────────────────────────────────────────────────


def load_theses() -> dict:
    """Manual thesis annotations, keyed by ticker or free-text cluster name."""
    if THESIS_FILE.exists():
        return json.loads(THESIS_FILE.read_text())
    return {}


def save_theses(theses: dict):
    THESIS_FILE.write_text(json.dumps(theses, indent=2))


def fetch_portfolio_weights() -> str:
    """
    Fetch open positions from Trading212, compute percentage weights locally,
    and return a privacy-safe summary. Absolute monetary values NEVER leave
    this function — only tickers and normalised percentages are returned.
    """
    if not T212_API_KEY and not T212_API_KEY_ID:
        return ""

    auth_header = t212_auth_header()

    try:
        resp = requests.get(
            f"{T212_BASE_URL}/api/v0/equity/positions",
            headers={"Authorization": auth_header},
            timeout=20,
        )
        resp.raise_for_status()
        positions = resp.json()
    except Exception as e:
        log.warning(f"Trading212 fetch failed: {e}")
        return ""

    if not positions:
        return ""

    # Compute total portfolio value locally (this number is discarded after)
    enriched = []
    total_value = 0.0
    for p in positions:
        wi = p.get("walletImpact") or {}
        value = wi.get("currentValue")
        if value is None:
            # Fall back to currentPrice * quantity if walletImpact missing
            value = (p.get("currentPrice") or 0) * (p.get("quantity") or 0)
        ticker = (p.get("instrument") or {}).get("ticker", "UNKNOWN")
        name = (p.get("instrument") or {}).get("name", "")
        pl = wi.get("unrealizedProfitLoss")
        enriched.append({"ticker": ticker, "name": name, "value": value, "pl": pl})
        total_value += value

    if total_value <= 0:
        return ""

    # Sort by weight descending, compute percentages
    enriched.sort(key=lambda x: x["value"], reverse=True)
    theses = load_theses()

    lines = []
    for e in enriched:
        weight = 100.0 * e["value"] / total_value
        if weight < 0.5:  # skip dust positions
            continue
        # P/L direction only — not the amount
        pl_dir = ""
        if e["pl"] is not None:
            pl_dir = " ▲" if e["pl"] > 0 else (" ▼" if e["pl"] < 0 else "")
        thesis = theses.get(e["ticker"], "")
        thesis_str = f" — {thesis}" if thesis else ""
        lines.append(
            f"- {e['ticker']} ({e['name']}): {weight:.1f}%{pl_dir}{thesis_str}"
        )

    # Append any cluster-level theses (not tied to a single ticker)
    cluster_theses = {k: v for k, v in theses.items() if k.startswith("cluster:")}
    cluster_lines = [
        f"- {k.replace('cluster:', '')}: {v}" for k, v in cluster_theses.items()
    ]

    out = (
        "Current portfolio weights (percentages only — amounts withheld):\n"
        + "\n".join(lines)
    )
    if cluster_lines:
        out += "\n\nThesis clusters:\n" + "\n".join(cluster_lines)
    return out


# ── Paper trading ──────────────────────────────────────────────────────────────

# Map a T212 instrument currency (and ISIN country for EUR) to a Stooq market suffix.
_STOOQ_SUFFIX = {"USD": "us", "GBP": "uk", "GBX": "uk"}
_STOOQ_EUR_BY_ISIN = {"DE": "de", "FR": "fr"}

# When a plain signal symbol matches several T212 listings (same base, different
# exchanges), prefer the US listing — signals carry US-style symbols (SHEL, EQNR,
# TSM) — then UK, then EUR markets. Lower rank wins.
_COUNTRY_PREFERENCE = {"US": 0, "GB": 1, "UK": 1, "DE": 2, "FR": 3}


def fetch_stooq_price(stooq_symbol: str) -> float | None:
    """Fetch the latest close price for a Stooq symbol (e.g. 'aapl.us').

    Returns None on network error or Stooq's 'N/D' not-found sentinel — callers MUST treat
    None as 'could not price' and skip, never substitute a guessed value.
    """
    url = f"https://stooq.com/q/l/?s={stooq_symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq fetch failed for {stooq_symbol}: {e}")
        return None
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return None
    cols = lines[1].split(",")  # Symbol,Date,Time,Open,High,Low,Close,Volume
    if len(cols) < 7 or cols[6] in ("N/D", ""):
        log.warning(f"Stooq returned no price for {stooq_symbol}")
        return None
    try:
        return float(cols[6])
    except ValueError:
        return None


def load_ticker_overrides() -> dict:
    """Manual T212-ticker -> Stooq-symbol overrides for instruments that don't map automatically."""
    if TICKER_MAP_FILE.exists():
        return json.loads(TICKER_MAP_FILE.read_text())
    return {}


def load_instruments_cache() -> dict:
    if INSTRUMENTS_CACHE_FILE.exists():
        return json.loads(INSTRUMENTS_CACHE_FILE.read_text())
    return {}


def refresh_instruments_cache(max_age_days: int = 14, force: bool = False) -> dict:
    """Refresh the T212 instrument metadata cache (ticker -> isin/currencyCode) if stale.

    One rate-limited call (1 req / 50s) returns the full catalogue. Returns the cache dict;
    returns the existing/empty cache unchanged when T212_API_KEY is unset or the call fails.
    """
    cache = load_instruments_cache()
    if not force and cache.get("fetched_at"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                cache["fetched_at"]
            )
            if age < timedelta(days=max_age_days):
                return cache
        except ValueError:
            pass
    if not T212_API_KEY and not T212_API_KEY_ID:
        return cache

    auth_header = t212_auth_header()

    try:
        resp = requests.get(
            f"{T212_BASE_URL}/api/v0/equity/metadata/instruments",
            headers={"Authorization": auth_header},
            timeout=30,
        )
        resp.raise_for_status()
        instruments = {
            i["ticker"]: {
                "isin": i.get("isin", ""),
                "currencyCode": i.get("currencyCode", ""),
            }
            for i in resp.json()
            if i.get("ticker")
        }
    except Exception as e:
        log.warning(f"Instrument cache refresh failed: {e}")
        return cache
    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "instruments": instruments,
    }
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_CACHE_FILE.write_text(json.dumps(cache))
    log.info(f"Instrument cache refreshed: {len(instruments)} instruments")
    return cache


def _match_instrument_by_base(symbol: str, instruments: dict) -> dict | None:
    """Find the cached T212 instrument whose base symbol matches `symbol`.

    Signals carry plain exchange symbols ('SHEL'), while T212 tickers are
    '<SYMBOL>_<COUNTRY>_EQ' (US) or '<SYMBOLl>_EQ' (LSE). The base is the part
    before the first '_'. When several listings share a base, prefer the US one
    (signals use US-style symbols). Returns the metadata dict or None.
    """
    want = symbol.split("_")[0].upper()
    candidates = []
    for tkr, meta in instruments.items():
        parts = tkr.split("_")
        if parts[0].upper() != want:
            continue
        country = parts[1].upper() if len(parts) > 2 else ""
        candidates.append((_COUNTRY_PREFERENCE.get(country, 9), tkr, meta))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def resolve_stooq_symbol(ticker: str, cache: dict, overrides: dict) -> str | None:
    """Map a signal ticker to a Stooq symbol (e.g. 'aapl.us').

    Resolution order:
      1. Manual override file (authoritative).
      2. Exact T212 ticker match in the instrument cache (e.g. 'AAPL_US_EQ').
      3. Base-symbol match: signals usually carry the plain exchange symbol
         ('SHEL', 'BP'), so match it against each instrument's base (the segment
         before the first '_'), preferring the US listing on ambiguity.
    The suffix is derived from the matched instrument's real currency (and ISIN
    country for EUR). Returns None when nothing resolves — callers skip and log.
    """
    if ticker in overrides:
        return overrides[ticker]
    instruments = cache.get("instruments", {})
    meta = instruments.get(ticker)
    if meta is None:
        meta = _match_instrument_by_base(ticker, instruments)
    if not meta:
        return None
    base = ticker.split("_")[0].lower()
    ccy = (meta.get("currencyCode") or "").upper()
    suffix = _STOOQ_SUFFIX.get(ccy)
    if suffix is None and ccy == "EUR":
        suffix = _STOOQ_EUR_BY_ISIN.get((meta.get("isin") or "")[:2].upper())
    if suffix is None:
        return None
    return f"{base}.{suffix}"


def load_paper_book() -> dict:
    if PAPER_BOOK_FILE.exists():
        return json.loads(PAPER_BOOK_FILE.read_text())
    return {"positions": []}


def save_paper_book(book: dict):
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_BOOK_FILE.write_text(json.dumps(book, indent=2))


def _signal_return(direction: str, entry: float, price: float) -> float:
    """Directional return ratio: +1 for bullish, -1 for bearish, FX/unit-neutral."""
    sign = 1.0 if direction == "bullish" else -1.0
    return sign * (price / entry - 1.0)


def _close_position_at_market(p: dict, day: str, reason: str) -> bool:
    """Close one open position at the current Stooq mark, stamping realized_return.

    Shared by the weekly horizon close, the /close command, and reversal closes.
    Returns False (leaving the position open) when Stooq can't price it.
    """
    price = fetch_stooq_price(p["stooq_symbol"])
    if price is None:
        return False
    ret = _signal_return(p["direction"], p["entry_price"], price)
    p["last_mark"] = {"date": day, "price": price, "return": ret}
    p["realized_return"] = ret
    p["status"] = "closed"
    p["close_reason"] = reason
    p["closed_date"] = day
    return True


def mode_paper():
    """Open paper positions from today's signals. Pure simulation — no money, no orders.

    Each medium/high-confidence directional signal with a resolvable ticker opens one notional
    paper position (deduped per ticker+direction). Prices come from Stooq; unmappable tickers,
    Stooq 'N/D', and macro/null-ticker signals are skipped and logged. Marking-to-market and
    closing happen in the weekly job.
    """
    log.info("=== PAPER ===")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_path = SIGNALS_DIR / f"signals-{today}.json"
    if not snap_path.exists():
        log.info("No signals snapshot for today — nothing to paper-trade")
        return

    signals = json.loads(snap_path.read_text()).get("signals", [])
    actionable = [
        s
        for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("confidence") in ("medium", "high")
        and s.get("ticker")
    ]
    if not actionable:
        log.info("No actionable signals today")
        return

    book = load_paper_book()
    open_keys = {
        (p["ticker"], p["direction"])
        for p in book["positions"]
        if p["status"] == "open"
    }
    cache = refresh_instruments_cache()
    overrides = load_ticker_overrides()

    opened = 0
    for s in actionable:
        ticker, direction = s["ticker"], s["direction"]
        opposite = "bearish" if direction == "bullish" else "bullish"

        # Reversal: a fresh opposite-direction call closes the standing position first.
        if (ticker, opposite) in open_keys:
            for p in book["positions"]:
                if (
                    p["status"] == "open"
                    and p["ticker"] == ticker
                    and p["direction"] == opposite
                    and _close_position_at_market(p, today, "reversal")
                ):
                    log.info(f"Paper reversal: closed {ticker} {opposite}")
            open_keys = {
                (q["ticker"], q["direction"])
                for q in book["positions"]
                if q["status"] == "open"
            }
            if (ticker, opposite) in open_keys:
                # Reversal close couldn't be priced — don't open the opposite yet.
                log.warning(
                    f"Paper skip: unpriced reversal for {ticker}; not opening {direction}"
                )
                continue

        if (ticker, direction) in open_keys:
            continue  # dedup: a position for this call is already open
        symbol = resolve_stooq_symbol(ticker, cache, overrides)
        if not symbol:
            log.warning(f"Paper skip: no Stooq symbol for {ticker}")
            continue
        price = fetch_stooq_price(symbol)
        if price is None:
            log.warning(f"Paper skip: no price for {ticker} ({symbol})")
            continue
        book["positions"].append(
            {
                "id": f"{today}:{ticker}:{direction}",
                "opened": today,
                "ticker": ticker,
                "stooq_symbol": symbol,
                "direction": direction,
                "confidence": s.get("confidence"),
                "topic": s.get("topic"),
                "thesis_ref": s.get("thesis_ref"),
                "rationale": s.get("rationale"),
                "entry_price": price,
                "entry_date": today,
                "status": "open",
                "close_reason": None,
                "closed_date": None,
                "checkpoints": {},
                "last_mark": None,
                "realized_return": None,
            }
        )
        open_keys.add((ticker, direction))
        opened += 1

    save_paper_book(book)
    log.info(f"Opened {opened} paper position(s)")


def mark_to_market(book: dict, today_str: str) -> dict:
    """Mark every open position to market, record crossed horizon checkpoints, close at 4w.

    Mutates and returns the book. A position whose Stooq price can't be fetched is left open
    and retried next run. All crossed-but-unrecorded checkpoints are recorded in one pass
    (covers a missed weekly run); closing happens once the 4w checkpoint is recorded.
    """
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        price = fetch_stooq_price(p["stooq_symbol"])
        if price is None:
            log.warning(
                f"MtM kept open (no price): {p['ticker']} ({p['stooq_symbol']})"
            )
            continue
        ret = _signal_return(p["direction"], p["entry_price"], price)
        p["last_mark"] = {"date": today_str, "price": price, "return": ret}
        days_open = (today - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        for label, threshold in PAPER_HORIZONS.items():
            if label not in p["checkpoints"] and days_open >= threshold:
                p["checkpoints"][label] = {
                    "date": today_str,
                    "price": price,
                    "return": ret,
                }
        if PAPER_CLOSE_HORIZON in p["checkpoints"]:
            p["status"] = "closed"
            p["close_reason"] = "horizon"
            p["closed_date"] = today_str
            p["realized_return"] = p["checkpoints"][PAPER_CLOSE_HORIZON]["return"]
    return book


def paper_scorecard(book: dict) -> str:
    """Build a Telegram-HTML paper scorecard: hit-rate and mean returns (percentages only)."""
    positions = book.get("positions", [])
    closed = [p for p in positions if p["status"] == "closed"]
    open_ = [p for p in positions if p["status"] == "open"]

    def _hit_rate(ps):
        rets = [
            p["realized_return"] for p in ps if p.get("realized_return") is not None
        ]
        if not rets:
            return None
        hits = sum(1 for r in rets if r > 0)
        return 100.0 * hits / len(rets), len(rets)

    lines = ["<b>🧪 PAPER SIGNALS SCORECARD</b>"]
    overall = _hit_rate(closed)
    if overall:
        rate, n = overall
        lines.append(f"• Realized hit-rate (at close): {rate:.0f}% of {n}")
        for conf in ("high", "medium"):
            sub = _hit_rate([p for p in closed if p.get("confidence") == conf])
            if sub:
                lines.append(f"  – {conf}: {sub[0]:.0f}% of {sub[1]}")
    for label in PAPER_HORIZONS:
        rets = [
            p["checkpoints"][label]["return"]
            for p in positions
            if label in p.get("checkpoints", {})
        ]
        if rets:
            lines.append(
                f"• Mean {label} return: {100.0 * sum(rets) / len(rets):+.1f}% (n={len(rets)})"
            )
    lines.append(f"• Open: {len(open_)} | Closed: {len(closed)}")
    recent = sorted(closed, key=lambda p: p.get("closed_date") or "", reverse=True)[:5]
    if recent:
        lines.append("Recently closed:")
        for p in recent:
            r = p.get("realized_return")
            rstr = f"{100 * r:+.1f}%" if r is not None else "n/a"
            lines.append(
                f"  • {p['ticker']} {p['direction']}: {rstr} ({p['close_reason']})"
            )
    return "\n".join(lines)


# ── Brief archive helpers ─────────────────────────────────────────────────────
def load_yesterday_brief() -> str:
    path = (
        BRIEFS_DIR
        / f"brief-{(datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
    )
    return path.read_text() if path.exists() else ""


def load_last_weekly_summary() -> str:
    """Load the most recent weekly summary if one exists."""
    WEEKLY_DIR.mkdir(exist_ok=True)
    summaries = sorted(WEEKLY_DIR.glob("week-*.md"), reverse=True)
    if summaries:
        return summaries[0].read_text()
    return ""


def load_last_n_briefs(n: int = 7) -> list[tuple[str, str]]:
    """Return up to n most recent daily briefs as (date_str, content) tuples."""
    BRIEFS_DIR.mkdir(exist_ok=True)
    files = sorted(BRIEFS_DIR.glob("brief-*.md"), reverse=True)[:n]
    result = []
    for f in reversed(files):  # chronological order
        date = f.stem.replace("brief-", "")
        result.append((date, f.read_text()))
    return result


# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a senior geopolitical and macroeconomic analyst producing a concise daily briefing for an investor.

The reader:
- Is Portuguese, based in the UK
- Has financial exposure to multiple countries and regions
- Tracks geopolitics as a leading indicator for markets, not as an end in itself
- Is familiar with constraint-based analysis (Papic/BCA style)
- Does not need hedging language or excessive caveats — be direct
- Prefers Reuters as a primary news source

Your job is to synthesise the provided source material into a structured morning brief.
Use the web search tool to fill gaps on the listed search topics — prioritise Reuters results.
Do not pad or repeat. If nothing significant happened on a topic, say so in one line."""


def build_daily_prompt(
    feed_content: str,
    web_content: str,
    chroma_context: str,
    yesterday_brief: str,
    weekly_summary: str,
    fb: dict,
    portfolio: str,
) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    search_list = "\n".join(f"- {t['search']}" for t in TOPICS)

    fb_lines = []
    if fb.get("focus"):
        fb_lines.append("FOCUS — go deeper on these: " + "; ".join(fb["focus"]))
    if fb.get("mute"):
        fb_lines.append(
            "MUTED — one-liner only, skip if nothing new: " + "; ".join(fb["mute"])
        )
    if fb.get("notes"):
        fb_lines.append("READER NOTES:\n" + "\n".join(f"  • {n}" for n in fb["notes"]))
    feedback_block = (
        ("\n## READER OVERRIDES\n" + "\n".join(fb_lines)) if fb_lines else ""
    )

    yesterday_block = ""
    if yesterday_brief:
        yesterday_block = f"""
## YESTERDAY'S BRIEF
For any section where the situation is materially unchanged from yesterday, replace the paragraph with a single sentence: "No significant change — [one-line summary]." Only write a full paragraph when something new or materially different has occurred.

{yesterday_brief[:2000]}
"""

    weekly_block = ""
    if weekly_summary:
        weekly_block = f"""
## WEEKLY CONTEXT (for trend awareness)
Use this to identify multi-day patterns, unresolved watch list items, and slow-moving trends. Do not repeat it — use it as background framing only.

{weekly_summary[:1000]}
"""

    portfolio_block = ""
    if portfolio:
        portfolio_block = f"""
## READER PORTFOLIO (privacy-safe — weights only, no amounts)
{portfolio}

When scoring news against these positions, flag only confirming or contradicting
evidence. Do NOT give buy/sell advice or price targets. Surface what is worth the
reader's attention; the reader decides what to do with it.
"""

    return f"""Today is {today} (UTC). Produce the morning brief.
{feedback_block}

## RSS / WEB SOURCE MATERIAL
{feed_content}
{web_content}

## PODCAST ANALYST CONTEXT
Relevant excerpts from an indexed archive of geopolitical/macro podcast episodes.
Use for analytical framing where appropriate. Cite the show name if drawing on a specific insight.
{chroma_context}

## WEB SEARCH TOPICS
Search for current developments on each before writing. Prioritise Reuters.
{search_list}
{yesterday_block}{weekly_block}{portfolio_block}

## OUTPUT FORMAT

Telegram HTML only. Allowed tags: <b>, <i>, <code>, <a href="...">
Use <b> for section headings. Bullets with •. No markdown. No # headers. No asterisks.
Output only the HTML — no preamble, no sign-off, no code fences.

<b>🌍 TOP STORIES</b>
- [3–5 bullets, only genuinely significant developments]

<b>🇺🇦 UKRAINE</b>
[paragraph, or: No significant change — one sentence]

<b>🇮🇷 US–IRAN / STRAIT OF HORMUZ</b>
[paragraph, or one-liner. Include BCA dashboard context if available.]

<b>🇰🇷🇰🇵 KOREA</b>
[paragraph, or: No significant change — one sentence]

<b>🇯🇵 JAPAN</b>
[paragraph, or: No significant change — one sentence]

<b>🇨🇳 CHINA</b>
[paragraph, or: No significant change — one sentence]

<b>📊 MACRO SIGNAL</b>
[paragraph if material; omit entirely if nothing significant]

<b>📌 POSITION SIGNALS</b>
- [news that confirms or challenges a held position or thesis — name the ticker/thesis and the signal direction. Omit the section entirely if nothing in today's news is materially relevant to the portfolio.]

<b>👁 WATCH LIST</b>
- [2–4 things to monitor in next 24–72h that could move markets]

After the WATCH LIST and a blank line, output the delimiter token below on its own line, exactly as written — it is a literal parsing marker, NOT a section divider, so reproduce it verbatim and do not shorten, restyle, or drop it:
@@@SIGNALS@@@
Then output a JSON array (and nothing else after it) capturing any position-relevant signals. Empty array if none. Schema:
[
  {{
    "ticker": "the primary listing symbol, e.g. SHEL or BP; null only for macro-level signals with no single tradable instrument",
    "topic": "short topic label, e.g. hormuz-disruption",
    "direction": "bullish | bearish | neutral",
    "thesis_ref": "the held thesis this bears on, or null",
    "confidence": "low | medium | high",
    "rationale": "one sentence, no more",
    "provenance": "which source/feed/search this came from"
  }}
]

Keep the entire brief under 600 words."""


WEEKLY_SYSTEM_PROMPT = """You are a senior geopolitical and macroeconomic analyst.
Your job is to produce a compact weekly synthesis from a set of daily briefs.
Be direct. Focus on patterns, trends, and unresolved situations — not a summary of each day."""


def build_weekly_prompt(briefs: list[tuple[str, str]]) -> str:
    brief_text = ""
    for date, content in briefs:
        brief_text += f"\n\n--- {date} ---\n{content[:1500]}"

    return f"""Below are the daily briefs from the past week. Produce a weekly synthesis.

{brief_text}

---

## OUTPUT FORMAT

Telegram HTML only. Allowed tags: <b>, <i>, <code>
Use <b> for section headings. Bullets with •. No markdown. No # headers.
Output only the HTML — no preamble, no sign-off, no code fences.

<b>📅 WEEK IN REVIEW</b>
2–3 sentences: the single most important geopolitical or macro development this week and why it matters.

<b>🔁 PERSISTENT SITUATIONS</b>
- [situations that have been present all week without resolution — note duration]
- [up to 4 bullets]

<b>📈 TRENDS TO WATCH</b>
- [patterns that emerged or intensified across multiple days this week]
- [up to 3 bullets]

<b>✅ RESOLVED / FADED</b>
- [things that were on the watch list earlier in the week but have since quieted — or note "none"]

Keep the entire summary under 400 words. This summary will be used as background context in next week's daily briefs."""


# Delimiters the model may emit before the signals JSON, primary first. The
# model is asked for @@@SIGNALS@@@; ---SIGNALS--- is kept for older archived runs.
_SIGNAL_MARKERS = ("@@@SIGNALS@@@", "---SIGNALS---")


def _find_trailing_json_array(text: str) -> tuple[int, list] | None:
    """Locate the last top-level JSON array in `text`.

    The signals block is always the final element of the model's output, so we
    anchor on the last ']' and try candidate '[' positions (leftmost first) until
    a substring parses as a list. Brackets in prose (e.g. citation markers) fail
    to parse and are skipped. Returns (start_index, parsed_list) or None.
    """
    end = text.rfind("]")
    if end == -1:
        return None
    start = text.find("[")
    while start != -1 and start <= end:
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            start = text.find("[", start + 1)
            continue
        if isinstance(value, list):
            return start, value
        start = text.find("[", start + 1)
    return None


def split_brief_and_signals(raw: str) -> tuple[str, list, str]:
    """Separate the prose brief from the trailing signals JSON block.

    The model is asked to emit a literal @@@SIGNALS@@@ delimiter before the JSON,
    but it sometimes restyles or drops it (notably collapsing it to a bare '---',
    or omitting it entirely). To stay robust we try, in order:
      1. a known delimiter (@@@SIGNALS@@@, or the legacy ---SIGNALS---),
      2. the trailing top-level JSON array on its own — recovering from a mangled
         or missing delimiter.

    Returns (prose, raw_signals, status) where status is one of:
      "ok"          — a JSON array was recovered (the array may be empty)
      "parse_error" — a known delimiter was present but no parseable array followed
      "no_marker"   — no delimiter and no trailing JSON array (model format failure)
    """
    for marker in _SIGNAL_MARKERS:
        if marker in raw:
            prose, _, signal_part = raw.partition(marker)
            found = _find_trailing_json_array(signal_part)
            if found is None:
                log.warning("Signals marker present but no parseable JSON array")
                return prose.strip(), [], "parse_error"
            return prose.strip(), found[1], "ok"

    # No known delimiter — the model dropped or mangled it (e.g. a bare '---').
    # Recover the trailing JSON array directly so signals aren't lost.
    found = _find_trailing_json_array(raw)
    if found is None:
        return raw.strip(), [], "no_marker"
    log.warning("Signals delimiter missing/mangled; recovered array by fallback")
    start, signals = found
    # Drop a bare '---' divider the model emitted in place of the delimiter.
    prose = re.sub(r"-{3,}\s*$", "", raw[:start].rstrip())
    return prose.strip(), signals, "ok"


# Synonym maps for coercing free-form model output to the known enums.
_DIRECTION_MAP = {
    "bullish": "bullish",
    "long": "bullish",
    "buy": "bullish",
    "positive": "bullish",
    "up": "bullish",
    "bearish": "bearish",
    "short": "bearish",
    "sell": "bearish",
    "negative": "bearish",
    "down": "bearish",
    "neutral": "neutral",
    "flat": "neutral",
    "hold": "neutral",
}
_CONFIDENCE_MAP = {
    "low": "low",
    "lo": "low",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "high": "high",
    "hi": "high",
}
_NULLISH = {"", "null", "none", "n/a", "na"}


def _nullish(value) -> str | None:
    """Map null-ish model values ('', 'null', 'none', ...) to None, else a stripped string."""
    if value is None or str(value).strip().lower() in _NULLISH:
        return None
    return str(value).strip()


def normalize_signals(raw_signals: list) -> tuple[list, int]:
    """Coerce free-form model signal output to the known 7-field schema.

    Validates/normalises the direction and confidence enums, requires a non-empty
    topic, nulls empty tickers/thesis_refs, and keeps only the known fields.
    Returns (clean_signals, dropped_count). A signal is dropped when its direction
    or confidence cannot be resolved, when it has no topic, or when it is not a dict.
    """
    clean, dropped = [], 0
    for item in raw_signals:
        if not isinstance(item, dict):
            dropped += 1
            continue
        direction = _DIRECTION_MAP.get(str(item.get("direction", "")).strip().lower())
        confidence = _CONFIDENCE_MAP.get(
            str(item.get("confidence", "")).strip().lower()
        )
        topic = str(item.get("topic", "")).strip()
        if direction is None or confidence is None or not topic:
            dropped += 1
            continue
        clean.append(
            {
                "ticker": _nullish(item.get("ticker")),
                "topic": topic,
                "direction": direction,
                "confidence": confidence,
                "thesis_ref": _nullish(item.get("thesis_ref")),
                "rationale": str(item.get("rationale", "")).strip(),
                "provenance": str(item.get("provenance", "")).strip(),
            }
        )
    return clean, dropped


def save_signals(signals: list, date_str: str, status: str = "ok", dropped: int = 0):
    """Persist signals as a dated snapshot and append to the rolling log.

    The dated snapshot is ALWAYS written (even for an empty signals list) so a quiet
    day is distinguishable from a missing run; `status` records whether the signals
    block parsed cleanly ('ok' | 'parse_error' | 'no_marker') and `dropped` how many
    malformed signals were discarded. The rolling log is appended only when signals exist.
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    # Dated snapshot — this is the signals.json a consumer (paper tracker, bot) reads
    snapshot = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "dropped": dropped,
        "signals": signals,
    }
    (SIGNALS_DIR / f"signals-{date_str}.json").write_text(
        json.dumps(snapshot, indent=2)
    )

    # Rolling log for the feedback review (only meaningful entries)
    if signals:
        rolling = SIGNALS_DIR / "signals-log.jsonl"
        with rolling.open("a") as f:
            for s in signals:
                f.write(json.dumps({**s, "date": date_str}) + "\n")

    log.info(
        f"Saved {len(signals)} signals for {date_str} (status={status}, dropped={dropped})"
    )


# ── Batch API ─────────────────────────────────────────────────────────────────
def submit_batch(system: str, prompt_user: str, custom_id: str) -> str:
    payload = {
        "requests": [
            {
                "custom_id": custom_id,
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "system": system,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": [{"role": "user", "content": prompt_user}],
                },
            }
        ]
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers=ANTHROPIC_HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    batch_id = resp.json()["id"]
    log.info(f"Batch submitted: {batch_id}")
    return batch_id


def submit_batch_no_search(system: str, prompt_user: str, custom_id: str) -> str:
    """Submit a batch job without web search — used for the weekly summary."""
    payload = {
        "requests": [
            {
                "custom_id": custom_id,
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt_user}],
                },
            }
        ]
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers=ANTHROPIC_HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    batch_id = resp.json()["id"]
    log.info(f"Weekly batch submitted: {batch_id}")
    return batch_id


def poll_batch(batch_id: str, max_wait_secs: int = 43200) -> str | None:
    url = f"https://api.anthropic.com/v1/messages/batches/{batch_id}"
    deadline = time.time() + max_wait_secs
    sleep = 60

    while time.time() < deadline:
        resp = requests.get(url, headers=ANTHROPIC_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("processing_status")
        log.info(f"Batch {batch_id}: {status}")

        if status == "ended":
            results_url = (
                data.get("results_url")
                or f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results"
            )
            return fetch_batch_results(results_url)

        time.sleep(min(sleep, 300))
        sleep = int(sleep * 1.5)

    log.error(f"Batch {batch_id} timed out")
    return None


def _dump_raw_batch_result(result: dict, joined_text: str) -> None:
    """Log a batch-result summary, and dump the full payload only on anomalies.

    The brief/signals files only retain post-parse output, so when the model's
    response is off (notably a max_tokens truncation that surfaces downstream as
    a signals parse_error) there is no record of what it actually returned. On
    every run this logs a one-line summary (stop_reason, content-block makeup,
    text length and tail). Only when the result looks anomalous — the model did
    not finish cleanly (stop_reason != "end_turn", e.g. "max_tokens") or emitted
    no text — does it also write the entire result object and the flattened text
    to /app/logs/debug, so the daily cron does not accumulate dumps on healthy
    days. Diagnostics must never break the run, so all failures here are swallowed.
    """
    try:
        message = result.get("result", {}).get("message", {})
        blocks = message.get("content", [])
        block_types: dict[str, int] = {}
        for b in blocks:
            t = b.get("type", "?")
            block_types[t] = block_types.get(t, 0) + 1
        stop_reason = message.get("stop_reason")
        log.warning(
            "Batch result: stop_reason=%s blocks=%s text_len=%d tail=%r",
            stop_reason,
            block_types,
            len(joined_text),
            joined_text[-400:],
        )
        if stop_reason == "end_turn" and joined_text.strip():
            return  # healthy run — summary line is enough, skip the file dump
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dump_dir = Path("/app/logs/debug")
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / f"batch-raw-{ts}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (dump_dir / f"batch-text-{ts}.txt").write_text(joined_text, encoding="utf-8")
        log.warning(
            f"Anomalous batch (stop_reason={stop_reason}) dumped to "
            f"{dump_dir}/batch-raw-{ts}.json"
        )
    except Exception as exc:  # never let diagnostics break the run
        log.warning(f"Failed to dump raw batch result: {exc}")


def fetch_batch_results(results_url: str) -> str | None:
    resp = requests.get(results_url, headers=ANTHROPIC_HEADERS, timeout=30, stream=True)
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            result = json.loads(line)
            if result.get("result", {}).get("type") == "succeeded":
                blocks = result["result"]["message"]["content"]
                text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
                _dump_raw_batch_result(result, text)
                return text
        except json.JSONDecodeError:
            continue
    log.error("No succeeded result in batch output")
    return None


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict | None:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None


def save_state(updates: dict):
    state = load_state() or {}
    state.update(updates)
    STATE_FILE.write_text(json.dumps(state))


def clear_batch_state():
    state = load_state() or {}
    for key in ("batch_id", "submitted_at", "date", "weekly_batch_id"):
        state.pop(key, None)
    STATE_FILE.write_text(json.dumps(state))


# ── Delivery ──────────────────────────────────────────────────────────────────
def sanitise_html(text: str) -> str:
    text = re.sub(r"```[a-z]*\n?", "", text)
    text = re.sub(
        r"</?(?!(?:" + "|".join(ALLOWED_TAGS) + r")(?:\s[^>]*)?>)[a-zA-Z][^>]*>",
        "",
        text,
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_html_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for part in re.split(r"(\n\n)", text):
        if len(current) + len(part) > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks


def deliver(text: str, header: str, archive_path: Path):
    """Sanitise, split, send via Telegram, and archive to disk."""
    clean = sanitise_html(text)
    chunks = split_html_message(f"{header}\n\n{clean}")

    ok = True
    for i, chunk in enumerate(chunks):
        if not telegram_send(chunk):
            log.error(f"Failed chunk {i + 1}/{len(chunks)}")
            ok = False
        elif len(chunks) > 1:
            time.sleep(0.5)

    log.info(f"Delivered ({len(chunks)} msg(s))" if ok else "Delivery had failures")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    plain = re.sub(r"<[^>]+>", "", text)
    archive_path.write_text(plain + "\n")
    log.info(f"Saved: {archive_path}")


# ── Modes ─────────────────────────────────────────────────────────────────────
def mode_submit():
    log.info("=== SUBMIT ===")
    process_telegram_commands()

    state = load_state() or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("date") == today and state.get("batch_id"):
        log.info(f"Already submitted today ({state['batch_id']}), skipping")
        return

    feed_content = (
        "\n".join(c for f in RSS_FEEDS if (c := fetch_rss(f))) or "(no RSS content)"
    )
    web_content = (
        "\n".join(c for s in WEB_SOURCES if (c := fetch_web_source(s)))
        or "(no web content)"
    )

    fb = load_feedback()
    chroma_context = build_chroma_context(fb)
    yesterday_brief = load_yesterday_brief()
    weekly_summary = load_last_weekly_summary()

    log.info(
        f"Chroma: {len(chroma_context)} chars | "
        f"Yesterday: {'yes' if yesterday_brief else 'no'} | "
        f"Weekly: {'yes' if weekly_summary else 'no'}"
    )

    portfolio = fetch_portfolio_weights()
    log.info(f"Portfolio: {'fetched' if portfolio else 'none'}")

    prompt = build_daily_prompt(
        feed_content,
        web_content,
        chroma_context,
        yesterday_brief,
        weekly_summary,
        fb,
        portfolio,
    )
    batch_id = submit_batch(SYSTEM_PROMPT, prompt, custom_id=f"newsbrief-{today}")
    save_state(
        {
            "batch_id": batch_id,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "date": today,
        }
    )


def mode_collect():
    log.info("=== COLLECT ===")
    state = load_state() or {}
    batch_id = state.get("batch_id")
    if not batch_id:
        log.error("No pending batch — run submit first")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = poll_batch(batch_id)
    if raw:
        brief, raw_signals, status = split_brief_and_signals(raw)
        signals, dropped = normalize_signals(raw_signals)
        if dropped or status != "ok":
            log.warning(f"Signals: status={status}, dropped={dropped}")
        deliver(
            brief,
            header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
            archive_path=BRIEFS_DIR / f"brief-{today}.md",
        )
        save_signals(signals, today, status=status, dropped=dropped)
        mode_paper()
        clear_batch_state()
    else:
        log.error("Could not retrieve brief — will retry next collect run")


def mode_weekly():
    """
    Generate a weekly summary from the last 7 daily briefs.
    Submit as a batch job (no web search needed — all content is local).
    Poll synchronously since this runs on a Sunday evening cron and
    we want the result available for Monday's brief.
    Run: 0 21 * * 0  (Sunday 9pm UTC)
    """
    log.info("=== WEEKLY ===")
    briefs = load_last_n_briefs(7)
    if not briefs:
        log.warning("No daily briefs found — skipping weekly summary")
        return

    log.info(
        f"Generating weekly summary from {len(briefs)} briefs: "
        f"{briefs[0][0]} → {briefs[-1][0]}"
    )

    prompt = build_weekly_prompt(briefs)
    batch_id = submit_batch_no_search(
        WEEKLY_SYSTEM_PROMPT,
        prompt,
        custom_id=f"weekly-{datetime.now(timezone.utc).strftime('%Y-W%W')}",
    )

    # Poll synchronously — weekly job runs at 9pm, result needed by next morning
    summary = poll_batch(batch_id, max_wait_secs=7200)
    if summary:
        week_label = datetime.now(timezone.utc).strftime("%Y-W%W")
        deliver(
            summary,
            header=f"📅 <b>Weekly Summary — W{datetime.now(timezone.utc).strftime('%W, %Y')}</b>",
            archive_path=WEEKLY_DIR / f"week-{week_label}.md",
        )
        log.info(f"Weekly summary saved: week-{week_label}.md")
    else:
        log.error("Weekly batch failed or timed out")

    # Mark the paper book to market regardless of the weekly-summary outcome
    refresh_instruments_cache(force=True)
    book = mark_to_market(
        load_paper_book(), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    save_paper_book(book)
    telegram_send(paper_scorecard(book))
    log.info("Paper book marked to market")


def mode_run():
    """Synchronous submit + collect for testing."""
    log.info("=== RUN (sync) ===")
    mode_submit()
    state = load_state() or {}
    batch_id = state.get("batch_id")
    if batch_id:
        raw = poll_batch(batch_id, max_wait_secs=3600)
        if raw:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            brief, raw_signals, status = split_brief_and_signals(raw)
            signals, dropped = normalize_signals(raw_signals)
            if dropped or status != "ok":
                log.warning(f"Signals: status={status}, dropped={dropped}")
            deliver(
                brief,
                header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
                archive_path=BRIEFS_DIR / f"brief-{today}.md",
            )
            save_signals(signals, today, status=status, dropped=dropped)
            mode_paper()
            clear_batch_state()


def mode_commands():
    """Process Telegram commands without submitting anything."""
    log.info("=== COMMANDS ===")
    process_telegram_commands()


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    dispatch = {
        "submit": mode_submit,
        "collect": mode_collect,
        "weekly": mode_weekly,
        "run": mode_run,
        "commands": mode_commands,
        "paper": mode_paper,
    }
    fn = dispatch.get(mode)
    if fn:
        fn()
    else:
        print("Usage: brief.py [submit|collect|weekly|run|commands]")
        sys.exit(1)
