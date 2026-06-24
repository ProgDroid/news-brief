#!/usr/bin/env python3
"""
newsbrief - Daily geopolitical/macro briefing via Claude Batch API

Modes:
  submit   — fetch feeds, query Chroma, submit batch job (~8pm UTC)
  collect  — poll for results, deliver via Telegram, save signals, open paper positions (~6am UTC)
  weekly   — weekly summary from last 7 briefs + mark the paper book to market (Sunday ~9pm UTC)
  commands — long-running bot daemon: real-time Telegram commands + buttons (long polling)
  run      — submit + collect synchronously (for testing)
  paper    — open paper positions from today's signals (also run inside collect)

The image entrypoint is `python brief.py`, so the MODE is the command argument. The
committed docker-compose.yml defines one service per mode from a single shared anchor.
Schedule the batch modes with your container scheduler or host cron, and run the
commands daemon as a persistent service (it's the sole getUpdates consumer — a second
poller would 409):
  0 20 * * *   docker compose run --rm newsbrief-submit
  0  6 * * *   docker compose run --rm newsbrief-collect
  0 21 * * 0   docker compose run --rm newsbrief-weekly
  docker compose up -d newsbrief-commands   # daemon, not cron
"""

import html
import os
import re
import json
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from common import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    REQUIRED_ENV,
    DATA_DIR,
    SIGNALS_DIR,
    log,
    ANTHROPIC_HEADERS,
    MODEL,
    T212_API_KEY_ID,
    T212_API_KEY,
    T212_BASE_URL,
    t212_auth_header,
    _write_json_atomic,
    _load_json_or,
    _redact,
    file_lock,
    telegram_send,
    telegram_alert,
    telegram_send_buttons,
    telegram_edit_text,
    telegram_answer_callback,
    telegram_set_my_commands,
    sanitise_html,
    split_html_message,
)
import trading
from trading import (
    load_book,
    save_book,
    _close_position_at_market,
    refresh_instruments_cache,
    mode_paper,
    mark_to_market,
    run_volume_monitor,
    load_watchlist,
    save_watchlist,
    resolve_watch_entry,
    price_position,
    _signal_return,
    build_market_pulse,
)
from validation import (
    performance_report,
    record_gate_history,
    performance_prompt_block,
    daily_trade_message,
)
from enrichment import (
    annotate_signals,
    build_enrichment,
    build_universe,
    is_enabled as enrichment_enabled,
    latest_signal_tickers,
    render_prompt_block,
)
from enrichment.models import bundles_from_dict


# Chroma MCP HTTP endpoint
# NOTE: This endpoint is called via HTTP POST with JSON-RPC 2.0 format.
# If you are running the MCP server locally or via a different transport,
# update CHROMA_MCP_URL in your .env accordingly.
CHROMA_MCP_URL = os.environ.get(
    "CHROMA_MCP_URL", "https://progdroid--podcast-mcp-server-mcp-server.modal.run/mcp"
)

MAX_TOKENS = 16384  # whole-turn budget; web-search loop + brief + signals JSON

STATE_FILE = DATA_DIR / "batch_state.json"
FEEDBACK_FILE = DATA_DIR / "feedback.json"
BRIEFS_DIR = DATA_DIR / "briefs"
WEEKLY_DIR = DATA_DIR / "weekly"

# Self-hosted Nitter (Twitter/X mirror) reachable on the container's Docker network.
# Default targets a service named `nitter` on Nitter's default internal port 8080;
# override NITTER_BASE_URL if your instance listens on a different host/port.
NITTER_BASE_URL = (
    os.environ.get("NITTER_BASE_URL", "http://nitter:8080").strip().rstrip("/")
)

THESIS_FILE = DATA_DIR / "theses.json"

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
        "kind": "wire",
    },
    {
        "name": "Reuters World",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fworld&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "geo",
        "kind": "wire",
    },
    {
        "name": "Sinica Podcast",
        "url": "https://sinica.substack.com/feed",
        "category": "china",
        "kind": "analyst",
    },
    {
        "name": "Un-Diplomatic",
        "url": "https://www.un-diplomatic.com/feed",
        "category": "geo",
        "kind": "analyst",
    },
    {
        "name": "Observing Japan",
        "url": "https://observingjapan.substack.com/feed",
        "category": "japan",
        "kind": "analyst",
    },
    {
        "name": "Pinecone Weekly Brief",
        "url": "https://pineconemacroresearch.substack.com/feed",
        "category": "geo",
        "kind": "analyst",
    },
    {
        "name": "Intersubjectively Transmissible",
        "url": "https://jashap.substack.com/feed",
        "category": "macro",
        "kind": "analyst",
    },
    {
        "name": "Marko Papic (@geo_papic)",
        # X killed unauthenticated scraping and rsshub.app's public route is dead;
        # served via the self-hosted Nitter on the container's Docker network.
        "url": f"{NITTER_BASE_URL}/geo_papic/rss",
        "category": "geo",
        "kind": "analyst",
    },
    {
        "name": "Jacob Shapiro (@jacobshap)",
        "url": f"{NITTER_BASE_URL}/jacobshap/rss",
        "category": "geo",
        "kind": "analyst",
    },
    # ── Region-native / primary sources (added 2026-06-14) ────────────────────
    {
        "name": "Al Jazeera",
        # Native RSS — 25 entries verified 2026-06-14
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "category": "geo",
        "kind": "regional",
    },
    {
        "name": "Kyiv Independent",
        # Native feed returns 0; proxy verified 56 entries 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Akyivindependent.com&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "ukraine",
        "kind": "regional",
    },
    {
        "name": "ISW Daily Assessment",
        # Proxy — 8 entries verified 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aunderstandingwar.org&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "ukraine",
        "kind": "primary",
    },
    {
        "name": "Yonhap (English)",
        # Proxy — 64 entries verified 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aen.yna.co.kr&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "korea",
        "kind": "regional",
    },
    {
        "name": "38 North",
        # Native feed returns 0; proxy (when:7d) verified 4 entries 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:7d+site%3A38north.org&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "korea",
        "kind": "primary",
    },
    {
        "name": "NHK World",
        # Proxy — 39 entries verified 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Awww3.nhk.or.jp%2Fnhkworld&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "japan",
        "kind": "regional",
    },
    {
        "name": "BOJ Statements",
        # Proxy (when:7d for low-frequency releases) — 7 entries verified 2026-06-14
        "url": "https://news.google.com/rss/search?q=when:7d+site%3Aboj.or.jp&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "japan",
        "kind": "primary",
    },
    {
        "name": "SCMP",
        # Native RSS — 50 entries verified 2026-06-14
        "url": "https://www.scmp.com/rss/91/feed",
        "category": "china",
        "kind": "regional",
    },
]

WEB_SOURCES = [
    {
        "name": "BCA Research — Iran Conflict Daily Dashboard",
        "url": "https://www.bcaresearch.com/collection/bcas-iran-conflict-daily-dashboard",
        "category": "iran",
        "kind": "regional",
    },
]

# ── Temporary sources (Telegram-managed, persisted on the volume) ─────────────
# RSS_FEEDS above is the always-on baseline, baked into the image. Temporary
# sources live in a JSON file on the persistent volume so they can be added or
# dropped (via /addsource, /sources, or by hand-editing the file in an emergency)
# WITHOUT rebuilding the image or restarting anything — the next submit run reads
# the file fresh and appends them to RSS_FEEDS. A malformed file degrades to "no
# temp sources" with a logged warning; it can never break the always-on feeds.
TEMP_SOURCES_FILE = DATA_DIR / "sources.json"
VALID_KINDS = ("wire", "analyst", "regional", "primary")


def _short_id(text: str) -> str:
    """Short stable id for inline-button callback_data, which Telegram caps at 64
    bytes — so a long ticker/URL/market-id can't be embedded directly. The picker
    renders buttons keyed by this hash and the callback re-derives it over the live
    list, so taps stay correct even if the list shifted between render and tap."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _source_id(url: str) -> str:
    """Stable id for a temp source, derived from its URL."""
    return _short_id(url)


def build_google_news_url(domain: str) -> str:
    """Build a Google News RSS proxy feed for a bare domain, matching the recipe
    the built-in regional feeds use (`when:2d site:<domain>`). Most temp sources
    are publisher domains whose native RSS is dead or absent, so this is the
    common path; a full feed URL is used verbatim instead."""
    q = quote_plus(f"when:2d site:{domain}")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def load_temp_sources() -> list[dict]:
    """Read + validate the temp-source file, returning feed dicts shaped exactly
    like RSS_FEEDS entries. Invalid entries are dropped with a warning rather than
    raised: one bad hand-edit must not take down the morning brief."""
    raw = _load_json_or(TEMP_SOURCES_FILE, [])
    if not isinstance(raw, list):
        log.warning("sources.json is not a list — ignoring all temp sources")
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning(f"temp source not an object, dropped: {entry!r}")
            continue
        name, url, category = entry.get("name"), entry.get("url"), entry.get("category")
        if not (name and url and category):
            log.warning(f"temp source missing name/url/category, dropped: {entry!r}")
            continue
        kind = entry.get("kind", "regional")
        if kind not in VALID_KINDS:
            kind = "regional"
        out.append(
            {
                "name": str(name),
                "url": str(url),
                "category": str(category).lower(),
                "kind": kind,
            }
        )
    return out


def add_temp_source(entry: dict) -> None:
    """Append a source (deduped by URL) under a lock so a concurrent edit can't
    lose it. `entry` must already be a valid {name,url,category,kind} dict."""
    with file_lock(TEMP_SOURCES_FILE):
        srcs = _load_json_or(TEMP_SOURCES_FILE, [])
        if not isinstance(srcs, list):
            srcs = []
        srcs = [
            s
            for s in srcs
            if not (isinstance(s, dict) and s.get("url") == entry["url"])
        ]
        srcs.append(entry)
        _write_json_atomic(TEMP_SOURCES_FILE, srcs)


def remove_temp_source(source_id: str) -> dict | None:
    """Remove the source whose id matches; return the removed dict, or None if no
    match. Locked read-modify-write, same as add."""
    with file_lock(TEMP_SOURCES_FILE):
        srcs = _load_json_or(TEMP_SOURCES_FILE, [])
        if not isinstance(srcs, list):
            return None
        removed, kept = None, []
        for s in srcs:
            if (
                removed is None
                and isinstance(s, dict)
                and _source_id(str(s.get("url", ""))) == source_id
            ):
                removed = s
            else:
                kept.append(s)
        if removed is not None:
            _write_json_atomic(TEMP_SOURCES_FILE, kept)
        return removed


# Default pinned topics — always rendered (at least a one-liner) when the
# reader has not customised the pin set via /pin /unpin. Matches the legacy
# hardcoded country sections so day-one behaviour is unchanged.
DEFAULT_PINS = ["ukraine", "iran", "korea", "japan", "china"]

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
    return _load_json_or(FEEDBACK_FILE, {"focus": [], "mute": [], "notes": []})


def save_feedback(fb: dict):
    _write_json_atomic(FEEDBACK_FILE, fb)


def resolved_pins(fb: dict) -> list[str]:
    """The active pin set: an explicit fb['pin'] list (even empty) wins;
    a missing key resolves to DEFAULT_PINS. Distinguishing absent-vs-empty
    lets /reset restore the defaults by dropping the key."""
    pins = fb.get("pin")
    return list(pins) if pins is not None else list(DEFAULT_PINS)


def feedback_summary(fb: dict) -> str:
    """Summary for Telegram echoes — html.escape()d because it is always
    embedded in parse_mode=HTML messages and contains stored user text."""
    lines = []
    if fb.get("focus"):
        lines.append("Focus: " + ", ".join(html.escape(f) for f in fb["focus"]))
    if fb.get("mute"):
        lines.append("Muted: " + ", ".join(html.escape(m) for m in fb["mute"]))
    if fb.get("notes"):
        lines.append(
            "Notes:\n" + "\n".join(f"  • {html.escape(n)}" for n in fb["notes"])
        )
    pins = resolved_pins(fb)
    if pins:
        lines.append("Pinned: " + ", ".join(html.escape(p) for p in pins))
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
  e.g. <code>/close AAPL_US_EQ</code> — or send <code>/close</code> alone to pick from a button list.

/watch [SYMBOL]
  Track an instrument for volume alerts (crypto/equity inferred).
  e.g. <code>/watch BTC</code> · <code>/watch prediction 0xMARKETID</code>

/unwatch [SYMBOL]
  Stop watching an instrument. Send <code>/unwatch</code> alone to pick from a button list.

/pin [topic]
  Always show a topic, even when quiet (at least a one-liner).
  e.g. <code>/pin taiwan</code>

/unpin [topic]
  Stop forcing a topic; it becomes dynamic again. Send <code>/unpin</code> alone to pick from a button list.

/addsource
  Add a temporary news source (guided — tap through category, kind, then paste a
  domain or feed URL). Temp sources merge into the daily brief until removed.

/sources — list temporary sources, each with a 🗑 remove button
/removesource [name]
  Remove a temp source by name (the button on /sources does the same).

/positions — open positions with live marks
/performance — performance report + go-live gate

/reset — clear all overrides
/status — show current overrides
/help — this message
"""


def telegram_get_updates(
    offset: int = 0, *, timeout: int = 0, allowed_updates: list | None = None
) -> list | None:
    """Fetch updates via long polling. Returns the update list (possibly empty) on
    success, or None on a transport/API error so the daemon can back off instead of
    hot-looping. `timeout` is the server-side long-poll hold; the HTTP read timeout
    is set above it so a held connection isn't cut early."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": timeout}
    if allowed_updates is not None:
        params["allowed_updates"] = json.dumps(allowed_updates)
    try:
        resp = requests.get(url, params=params, timeout=timeout + 15)
        if not resp.ok:
            # 409 = another getUpdates consumer is running (e.g. a stray daemon or a
            # leftover submit poll). Surface it loudly; it self-resolves once the
            # other poller stops.
            log.warning(f"getUpdates {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json().get("result", [])
    except Exception as e:
        log.warning(f"getUpdates failed: {_redact(str(e))}")
        return None


# ── /addsource wizard + /sources (interactive, button-driven) ─────────────────
# In-memory only: a button wizard needs multi-step state, but the daemon is the
# sole process and the flow lasts seconds, so persisting it isn't worth it — a
# restart mid-wizard just drops the half-built source and the user re-taps.
_WIZARD: dict[str, dict] = {}
_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$", re.I)


def _known_categories() -> list[str]:
    cats = {f["category"] for f in RSS_FEEDS} | {
        s["category"] for s in load_temp_sources()
    }
    return sorted(cats)


def _btn_rows(buttons: list[tuple[str, str]], per_row: int = 3) -> list[list[dict]]:
    """Chunk (label, callback_data) pairs into inline-keyboard rows."""
    btns = [{"text": t, "callback_data": d} for t, d in buttons]
    return [btns[i : i + per_row] for i in range(0, len(btns), per_row)]


_CANCEL_ROW = [{"text": "❌ Cancel", "callback_data": "as:cancel"}]


def _wizard_start(chat_id: str) -> None:
    buttons = [(c, f"as:cat:{c}") for c in _known_categories()]
    buttons.append(("✏️ Other", "as:cat:__other__"))
    rows = _btn_rows(buttons) + [_CANCEL_ROW]
    msg_id = telegram_send_buttons("➕ <b>Add a source</b>\n\nPick a category:", rows)
    _WIZARD[chat_id] = {"step": "category", "msg_id": msg_id}


def _wizard_kind_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "kind"
    rows = _btn_rows([(k, f"as:kind:{k}") for k in VALID_KINDS], per_row=2) + [
        _CANCEL_ROW
    ]
    telegram_edit_text(
        w["msg_id"],
        f"➕ <b>Add a source</b>\nCategory: <b>{html.escape(w['category'])}</b>\n\n"
        "What kind of source is it?",
        rows,
    )


def _wizard_url_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "url"
    telegram_edit_text(
        w["msg_id"],
        f"➕ <b>Add a source</b>\nCategory: <b>{html.escape(w['category'])}</b> · "
        f"Kind: <b>{w['kind']}</b>\n\n"
        "Now send the source as a new message — either:\n"
        "• a bare domain, e.g. <code>timesofisrael.com</code> "
        "(I'll build a Google News feed), or\n"
        "• a full RSS/Atom URL, e.g. <code>https://site.com/feed.xml</code>",
        [],
    )


def _wizard_confirm_prompt(chat_id: str) -> None:
    w = _WIZARD[chat_id]
    w["step"] = "confirm"
    rows = [
        [
            {"text": "✅ Add", "callback_data": "as:confirm"},
            {"text": "✏️ Rename", "callback_data": "as:rename"},
            {"text": "❌ Cancel", "callback_data": "as:cancel"},
        ]
    ]
    telegram_edit_text(
        w["msg_id"],
        "➕ <b>Add this source?</b>\n\n"
        f"Name: <b>{html.escape(w['name'])}</b>\n"
        f"Category: <b>{html.escape(w['category'])}</b> · Kind: <b>{w['kind']}</b>\n"
        f"URL: <code>{html.escape(w['url'])}</code>",
        rows,
    )


def _wizard_handle_text(chat_id: str, text: str) -> None:
    """Handle a free-text reply while a wizard is awaiting one (category name,
    domain/URL, or a rename). No-op if the step doesn't expect text."""
    w = _WIZARD.get(chat_id)
    if not w:
        return
    step = w.get("step")
    if step == "category_text":
        w["category"] = text.strip().lower()
        _wizard_kind_prompt(chat_id)
    elif step == "rename":
        w["name"] = text.strip()
        _wizard_confirm_prompt(chat_id)
    elif step == "url":
        raw = text.strip()
        if "://" in raw:
            w["url"] = raw
            w["name"] = urlsplit(raw).netloc or raw
        elif _DOMAIN_RE.match(raw):
            w["url"] = build_google_news_url(raw)
            w["name"] = raw
        else:
            telegram_send(
                "⚠️ That's neither a domain nor a URL. Send something like "
                "<code>timesofisrael.com</code> or "
                "<code>https://site.com/feed.xml</code>."
            )
            return
        _wizard_confirm_prompt(chat_id)


def _sources_render(message_id: int | None = None) -> None:
    """Render the temp-source list with a 🗑 button per source. When message_id is
    given the existing message is edited in place (used to refresh after a removal),
    otherwise a fresh message is sent (the /sources command)."""
    srcs = load_temp_sources()
    if not srcs:
        text = "🗂 No temporary sources. Add one with /addsource."
        if message_id:
            telegram_edit_text(message_id, text, [])
        else:
            telegram_send(text)
        return
    lines = ["🗂 <b>Temporary sources</b>"]
    rows = []
    for s in srcs:
        lines.append(
            f"• <b>{html.escape(s['name'])}</b> [{s['kind']}] "
            f"({html.escape(s['category'])})"
        )
        rows.append(
            [
                {
                    "text": f"🗑 {s['name'][:48]}",
                    "callback_data": f"rmsrc:{_source_id(s['url'])}",
                }
            ]
        )
    text = "\n".join(lines)
    if message_id:
        telegram_edit_text(message_id, text, rows)
    else:
        telegram_send_buttons(text, rows)


# ── Button pick-lists (/close, /unwatch, /unpin) + /reset confirm ─────────────
# Each of these commands takes a target that's tedious/error-prone to type exactly
# (a paper ticker, a watchlist symbol, a pinned topic). The no-arg form renders the
# current list as tappable buttons; the text form (e.g. `/close AAPL_US_EQ`) still
# works. Buttons are keyed by _short_id over a stable field and re-resolved against
# the live list on tap, so a list that shifted between render and tap stays correct.
def _picker_send(text: str, rows: list, message_id: int | None) -> None:
    if message_id:
        telegram_edit_text(message_id, text, rows)
    else:
        telegram_send_buttons(text, rows)


def _picker_empty(text: str, message_id: int | None) -> None:
    if message_id:
        telegram_edit_text(message_id, text, [])
    else:
        telegram_send(text)


def _pos_ticker(p: dict) -> str:
    """A position's display/close key — ticker if present, else the raw instrument
    (some positions, e.g. prediction markets, carry no separate ticker)."""
    return p.get("ticker") or p.get("instrument", "")


def _close_ticker(tkr: str) -> None:
    """Close all open paper positions for one ticker at the current mark. Shared by
    the `/close TICKER` text command and the close-picker button."""
    # Hold the book lock across load->close->save so a concurrent mode_paper
    # (collect) write can't clobber this manual close.
    with file_lock(trading.BOOK_FILE):
        book = load_book()
        matches = [
            p
            for p in book["positions"]
            if p["status"] == "open" and _pos_ticker(p) == tkr
        ]
        if not matches:
            telegram_send(f"No open paper position for <b>{html.escape(tkr)}</b>.")
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed_n = sum(_close_position_at_market(p, day, "manual") for p in matches)
        if closed_n:
            save_book(book)
            telegram_send(
                f"✅ Closed {closed_n} paper position(s) for "
                f"<b>{html.escape(tkr)}</b> (manual)."
            )
        else:
            telegram_send(f"⚠️ Couldn't price {html.escape(tkr)} — left open.")


def _close_picker_render(message_id: int | None = None) -> None:
    book = load_book()
    seen, rows = set(), []
    for p in book["positions"]:
        if p.get("status") != "open":
            continue
        tkr = _pos_ticker(p)
        if tkr in seen:
            continue  # one button per ticker; _close_ticker closes all its lots
        seen.add(tkr)
        rows.append([{"text": f"❌ {tkr}", "callback_data": f"close:{_short_id(tkr)}"}])
    if not rows:
        _picker_empty("No open positions to close.", message_id)
        return
    _picker_send("📂 <b>Close which position?</b>", rows, message_id)


def _watch_key(item: dict) -> str:
    return f"{item.get('asset_class', '')}|{item.get('instrument', '')}"


def _watchlist_picker_render(message_id: int | None = None) -> None:
    items = load_watchlist()["items"]
    if not items:
        _picker_empty("👁️ Watchlist is empty.", message_id)
        return
    rows = [
        [
            {
                "text": f"🚫 {(i.get('raw') or i.get('instrument', '?'))[:48]}",
                "callback_data": f"unwatch:{_short_id(_watch_key(i))}",
            }
        ]
        for i in items
    ]
    _picker_send("👁️ <b>Unwatch which instrument?</b>", rows, message_id)


def _pins_picker_render(fb: dict, message_id: int | None = None) -> None:
    pins = resolved_pins(fb)
    if not pins:
        _picker_empty("📌 No pinned topics.", message_id)
        return
    rows = [
        [{"text": f"📍 {t}", "callback_data": f"unpin:{_short_id(t)}"}] for t in pins
    ]
    _picker_send("📌 <b>Unpin which topic?</b>", rows, message_id)


def _handle_callback_query(cb: dict, fb: dict | None = None) -> dict | None:
    """Handle an inline-button tap and return the (possibly mutated) feedback dict.
    Covers /sources removals, the /addsource wizard, the /close /unwatch /unpin
    pickers and /reset confirmation. Every callback is answered (Telegram shows a
    spinner on the button until then). fb is threaded so button actions that change
    overrides (unpin, reset) stay consistent with the daemon's in-memory copy."""
    cb_id = cb.get("id", "")
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    data = cb.get("data", "")
    telegram_answer_callback(cb_id)
    if chat_id != str(TELEGRAM_CHAT_ID):
        return fb
    msg_id = cb.get("message", {}).get("message_id")

    # ── Non-wizard pickers (must come before the wizard-state guard below) ──
    if data.startswith("rmsrc:"):
        removed = remove_temp_source(data[len("rmsrc:") :])
        if removed:
            telegram_send(
                f"🗑 Removed temp source: <b>{html.escape(removed.get('name', ''))}</b>"
            )
        _sources_render(msg_id)  # refresh the list in place
        return fb

    if data.startswith("close:"):
        h = data[len("close:") :]
        tkr = next(
            (
                _pos_ticker(p)
                for p in load_book()["positions"]
                if p.get("status") == "open" and _short_id(_pos_ticker(p)) == h
            ),
            None,
        )
        if tkr:
            _close_ticker(tkr)
        _close_picker_render(msg_id)
        return fb

    if data.startswith("unwatch:"):
        h = data[len("unwatch:") :]
        wl = load_watchlist()
        item = next((i for i in wl["items"] if _short_id(_watch_key(i)) == h), None)
        if item:
            wl["items"] = [i for i in wl["items"] if i is not item]
            save_watchlist(wl)
            telegram_send(
                f"🚫 Unwatched <b>"
                f"{html.escape(item.get('raw') or item.get('instrument', ''))}</b>."
            )
        _watchlist_picker_render(msg_id)
        return fb

    if data.startswith("unpin:"):
        h = data[len("unpin:") :]
        if fb is not None:
            fb.setdefault("pin", list(DEFAULT_PINS))
            topic = next((t for t in fb["pin"] if _short_id(t) == h), None)
            if topic:
                fb["pin"].remove(topic)
                telegram_send(f"📍 Unpinned: <b>{html.escape(topic)}</b>.")
            _pins_picker_render(fb, msg_id)
        return fb

    if data == "reset:yes":
        fb = {"focus": [], "mute": [], "notes": []}
        telegram_edit_text(msg_id, "🔄 All overrides cleared.", [])
        return fb
    if data == "reset:no":
        telegram_edit_text(msg_id, "Reset cancelled — nothing changed.", [])
        return fb

    # ── /addsource wizard ──
    if data == "as:cancel":
        w = _WIZARD.pop(chat_id, None)
        if w:
            telegram_edit_text(w["msg_id"], "❌ Cancelled.", [])
        return fb

    w = _WIZARD.get(chat_id)
    if not w:
        return fb  # stale buttons from a finished/cancelled wizard

    if data.startswith("as:cat:"):
        cat = data[len("as:cat:") :]
        if cat == "__other__":
            w["step"] = "category_text"
            telegram_edit_text(
                w["msg_id"], "➕ <b>Add a source</b>\n\nType the category name:", []
            )
        else:
            w["category"] = cat
            _wizard_kind_prompt(chat_id)
    elif data.startswith("as:kind:"):
        w["kind"] = data[len("as:kind:") :]
        _wizard_url_prompt(chat_id)
    elif data == "as:rename":
        w["step"] = "rename"
        telegram_edit_text(
            w["msg_id"], "➕ <b>Add a source</b>\n\nType a display name:", []
        )
    elif data == "as:confirm":
        entry = {
            "name": w["name"],
            "url": w["url"],
            "category": w["category"],
            "kind": w["kind"],
        }
        add_temp_source(entry)
        _WIZARD.pop(chat_id, None)
        telegram_edit_text(
            w["msg_id"],
            f"✅ Added <b>{html.escape(entry['name'])}</b> "
            f"[{entry['kind']}] ({html.escape(entry['category'])}).",
            [],
        )
    return fb


def _handle_telegram_update(update: dict, fb: dict) -> dict:
    """Apply one Telegram update to the feedback dict; returns the (possibly
    replaced) feedback dict. User-provided text is html.escape()d before being
    echoed back — Telegram 400s the whole message on malformed HTML, so a note
    like 'watch JPY <155' would otherwise silently kill the confirmation."""
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != str(TELEGRAM_CHAT_ID):
        return fb

    # A free-text reply feeding an in-progress /addsource wizard (pasting a
    # domain/URL or typing a name/category). A new "/command" falls through and,
    # if it starts another action, the wizard is simply abandoned (re-tap to redo).
    if chat_id in _WIZARD and not text.startswith("/"):
        _wizard_handle_text(chat_id, text)
        return fb

    if text.startswith("/focus "):
        item = text[7:].strip()
        if item and item not in fb["focus"]:
            fb["focus"].append(item)
        telegram_send(
            f"✅ Focus added: <b>{html.escape(item)}</b>\n\n{feedback_summary(fb)}"
        )

    elif text.startswith("/mute "):
        item = text[6:].strip().lower()
        if item and item not in fb["mute"]:
            fb["mute"].append(item)
        telegram_send(f"🔇 Muted: <b>{html.escape(item)}</b>\n\n{feedback_summary(fb)}")

    elif text.startswith("/note "):
        note = text[6:].strip()
        if note:
            fb["notes"].append(note)
        telegram_send(
            f"📝 Note added: <i>{html.escape(note)}</i>\n\n{feedback_summary(fb)}"
        )

    elif text == "/reset":
        telegram_send_buttons(
            "🔄 Clear all overrides (focus, mute, notes) and restore default pins?",
            [
                [
                    {"text": "✅ Yes, clear", "callback_data": "reset:yes"},
                    {"text": "❌ No", "callback_data": "reset:no"},
                ]
            ],
        )

    elif text == "/status":
        telegram_send(f"<b>Current overrides</b>\n\n{feedback_summary(fb)}")

    elif text == "/addsource":
        _wizard_start(chat_id)

    elif text == "/sources":
        _sources_render()

    elif text.startswith("/removesource "):
        name = text[len("/removesource ") :].strip()
        match = next(
            (s for s in load_temp_sources() if s["name"].lower() == name.lower()), None
        )
        if match and remove_temp_source(_source_id(match["url"])):
            telegram_send(f"🗑 Removed temp source: <b>{html.escape(match['name'])}</b>")
        else:
            telegram_send(
                f"No temp source named <b>{html.escape(name)}</b>. "
                "Use /sources to see the list."
            )

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
            telegram_send(
                f"📌 Thesis set: <b>{html.escape(key.strip())}</b> — "
                f"{html.escape(val.strip())}"
            )
        else:
            telegram_send("Format: <code>/thesis TICKER = your thesis</code>")

    elif text.startswith("/dig "):
        query = text[5:].strip()
        since = None
        m = re.match(r"since:(\d{4}-\d{2}-\d{2})\s+(.*)", query)
        if m:
            since, query = m.group(1), m.group(2)
        telegram_send(f"🔎 Digging into: <i>{html.escape(query)}</i>…")
        answer = run_dig(query, since=since)
        for chunk in split_html_message(sanitise_html(answer)):
            telegram_send(chunk)
            time.sleep(0.4)

    elif text == "/close":
        _close_picker_render()

    elif text.startswith("/close "):
        _close_ticker(text[7:].strip())

    elif text.startswith("/watch "):
        body = text[7:].strip()
        parts = body.split(maxsplit=1)
        if parts and parts[0] in ("equity", "crypto", "prediction") and len(parts) == 2:
            ac, token = parts[0], parts[1].strip()
        else:
            ac, token = None, body
        entry = resolve_watch_entry(token, asset_class=ac)
        if entry is None:
            telegram_send(
                f"⚠️ Couldn't resolve <b>{html.escape(token)}</b>"
                + (
                    " (prediction needs an explicit market id: <code>/watch prediction &lt;id&gt;</code>)"
                    if ac is None
                    else ""
                )
            )
        else:
            wl = load_watchlist()
            dup = any(
                i.get("asset_class") == entry["asset_class"]
                and i.get("instrument") == entry["instrument"]
                for i in wl["items"]
            )
            if not dup:
                entry["added"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                wl["items"].append(entry)
                save_watchlist(wl)
            telegram_send(
                f"👁️ Watching <b>{html.escape(entry['raw'])}</b> "
                f"({entry['asset_class']} → <code>{html.escape(entry['instrument'])}</code>)"
                + ("" if not dup else " — already watched")
            )

    elif text == "/unwatch":
        _watchlist_picker_render()

    elif text.startswith("/unwatch "):
        token = text[9:].strip()
        wl = load_watchlist()
        before = len(wl["items"])
        wl["items"] = [
            i for i in wl["items"] if (i.get("raw") or "").lower() != token.lower()
        ]
        if len(wl["items"]) < before:
            save_watchlist(wl)
            telegram_send(f"🚫 Unwatched <b>{html.escape(token)}</b>.")
        else:
            telegram_send(f"<b>{html.escape(token)}</b> is not on the watchlist.")

    elif text.startswith("/pin "):
        topic = text[5:].strip().lower()
        fb.setdefault("pin", list(DEFAULT_PINS))
        if topic and topic not in fb["pin"]:
            fb["pin"].append(topic)
        telegram_send(
            f"📌 Pinned: <b>{html.escape(topic)}</b> — always shown.\n\n"
            f"{feedback_summary(fb)}"
        )

    elif text == "/unpin":
        _pins_picker_render(fb)

    elif text.startswith("/unpin "):
        topic = text[7:].strip().lower()
        fb.setdefault("pin", list(DEFAULT_PINS))
        if topic in fb["pin"]:
            fb["pin"].remove(topic)
            telegram_send(
                f"📍 Unpinned: <b>{html.escape(topic)}</b>.\n\n{feedback_summary(fb)}"
            )
        else:
            telegram_send(f"<b>{html.escape(topic)}</b> is not pinned.")

    elif text == "/positions":
        book = load_book()
        opens = [p for p in book["positions"] if p.get("status") == "open"]
        if not opens:
            telegram_send("No open positions.")
        else:
            by_class: dict[str, list[str]] = {}
            for p in opens:
                mark = price_position(p)
                if mark is None:
                    line = (
                        f"  – {html.escape(p.get('ticker', p['instrument']))}: mark —"
                    )
                else:
                    ret = _signal_return(p["direction"], p["entry_price"], mark)
                    line = (
                        f"  – {html.escape(p.get('ticker', p['instrument']))}: "
                        f"{100 * ret:+.1f}%"
                    )
                by_class.setdefault(p.get("asset_class", "equity"), []).append(line)
            lines = ["<b>📂 Open positions</b>"]
            for ac in ("equity", "crypto", "prediction"):
                if by_class.get(ac):
                    lines.append(f"<b>{ac}</b>")
                    lines.extend(by_class[ac])
            telegram_send("\n".join(lines))

    elif text == "/performance":
        for chunk in split_html_message(performance_report(load_book())):
            telegram_send(chunk)
            time.sleep(0.4)

    else:
        telegram_send("Unknown command — send /help for options.")

    return fb


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
        # Fetch with requests rather than letting feedparser fetch: feedparser
        # uses no socket timeout, so one hung feed (a wedged Nitter, not a dead
        # one) would block the whole submit run indefinitely. This also gives
        # real HTTP status handling instead of spelunking bozo_exception.
        resp = requests.get(
            feed["url"],
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; newsbrief/1.0)"},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            bozo_exc = getattr(parsed, "bozo_exception", None)
            log.warning(f"No entries: {feed['name']} ({bozo_exc or 'empty feed'})")
            return ""
        kind = feed.get("kind", "wire").upper()
        lines = [f"\n### {feed['name']} [{kind}] ({feed['category'].upper()})"]
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
def _chroma_call(payload: dict, log_label: str) -> list[str]:
    """POST a JSON-RPC tools/call to the podcast Chroma MCP server and return the
    plain-text excerpts. Shared transport for query_chroma / query_chroma_latest.
    The server speaks JSON-RPC 2.0 over MCP's Streamable HTTP transport, which
    requires the client to advertise it accepts both application/json and
    text/event-stream; omitting that Accept header yields 406 Not Acceptable.
    """
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
        log.warning(f"Chroma failed '{log_label}': {e}")
        return []


def query_chroma(query: str, n_results: int = 2) -> list[str]:
    """Semantic podcast search via the Chroma MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_podcasts",
            "arguments": {"query": query, "n_results": n_results},
        },
    }
    return _chroma_call(payload, query)


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
    return _chroma_call(payload, topic)


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
    return _load_json_or(THESIS_FILE, {})


def save_theses(theses: dict):
    _write_json_atomic(THESIS_FILE, theses)


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
- Prefers Reuters as a primary news source for facts
- Trades equities and major cryptocurrencies (BTC, ETH, and other large-cap coins); surface directional crypto calls the same way as equities when news warrants

Your job is to synthesise the provided source material into a structured morning brief.
Anchor facts on Reuters/wire sources, but LEAD your interpretation with forward-looking,
anticipatory material — regional analysts, primary statements, podcast framing, and the
market action provided — over backward-looking wire recap. Use the web search tool to fill
gaps on the listed topics. Do not echo headlines the market has already priced. Do not pad
or repeat. If nothing significant happened on a topic, say so in one line."""


def build_daily_prompt(
    feed_content: str,
    web_content: str,
    chroma_context: str,
    yesterday_brief: str,
    weekly_summary: str,
    fb: dict,
    portfolio: str,
    perf_block: str = "",
    market_block: str = "",
    enrichment_block: str = "",
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
    pins = resolved_pins(fb)
    if pins:
        fb_lines.append(
            "PINNED — always include at least a one-line pulse for, in this order: "
            + ", ".join(pins)
        )
    feedback_block = (
        ("\n## READER OVERRIDES\n" + "\n".join(fb_lines)) if fb_lines else ""
    )

    yesterday_block = ""
    if yesterday_brief:
        yesterday_block = f"""
## YESTERDAY'S BRIEF
For any section where the situation is materially unchanged from yesterday, replace the paragraph with a single sentence: "No significant change — [one-line summary]." This applies to standing analytical frames too — named theses, recurring podcast framings, and one-time events already reported: state them in at most one clause and never re-explain them. Only write a full paragraph when something new or materially different has occurred.

{yesterday_brief[:6000]}
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

    enrichment_section = f"\n{enrichment_block}\n" if enrichment_block else ""

    market_section = (
        f"\n## MARKET PULSE (what moved — you supply the why)\n{market_block}\n"
        if market_block
        else ""
    )

    return f"""Today is {today} (UTC). Produce the morning brief.
{feedback_block}

## SOURCE MATERIAL
Each source is tagged [WIRE|ANALYST|REGIONAL|PRIMARY]. Treat WIRE as the record of
what happened (anchor facts here), but lead your interpretation with ANALYST /
REGIONAL / PRIMARY material and the market action below. Compress recap; do not
echo a headline the market has already priced.
{feed_content}
{web_content}

## PODCAST ANALYST CONTEXT
Relevant excerpts from an indexed archive of geopolitical/macro podcast episodes.
Use for forward-looking analytical framing. Cite the show name if drawing on a specific insight.
{chroma_context}
{market_section}
## WEB SEARCH TOPICS
Search for current developments on each before writing. Anchor facts on Reuters; for
local colour and forward framing, prefer the region-native sources above.
{search_list}
{yesterday_block}{weekly_block}{portfolio_block}{enrichment_section}
{perf_block}

## OUTPUT FORMAT

Telegram HTML only. Allowed tags: <b>, <i>, <code>, <a href="...">
Use <b> for section headings. Bullets with •. No markdown. No # headers. No asterisks.
Output only the HTML — no preamble, no sign-off, no code fences.

Structure — a fixed spine with a dynamic middle:

<b>🌍 TOP STORIES</b>
- [3–5 bullets, only genuinely significant developments]

<b>📈 MARKET PULSE — WHAT MOVED</b>
[2–4 bullets: the notable moves above and the likely why. Flag any move NOT explained
by today's news as a potential early signal. Omit only if no market data was provided.]

[DYNAMIC TOPIC SECTIONS — your discretion:
• Render a section for EVERY pinned topic listed under READER OVERRIDES, even if quiet
  (a quiet pin collapses to: "No significant change — one sentence"). Never drop a pin.
• ALSO add a section for any UNPINNED topic that is materially significant today.
• Order by significance. Use a <b>flag + NAME</b> heading per section.]

<b>📊 MACRO SIGNAL</b>
[paragraph if material; omit entirely if nothing significant]

<b>📌 POSITION SIGNALS</b>
- [news that confirms or challenges a held position or thesis — name the ticker/thesis and
  the signal direction. Omit the section entirely if nothing is materially relevant.]

<b>👁 WATCH / FORWARD</b>
- [2–4 forward-looking things to monitor in the next 24–72h that could move markets]

After the WATCH / FORWARD section and a blank line, output the delimiter token below on its
own line, exactly as written — it is a literal parsing marker, NOT a section divider, so
reproduce it verbatim and do not shorten, restyle, or drop it:
@@@SIGNALS@@@
Then output a JSON array (and nothing else after it) capturing any position-relevant signals.
Empty array if none. Schema:
[
  {{
    "ticker": "the primary listing symbol — e.g. SHEL or BP for equities, BTC or ETH for crypto; null only for macro-level signals with no single tradable instrument",
    "asset_class": "equity | crypto — equity for stocks/ETFs, crypto for major coins; default to equity if unsure",
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
_ASSET_CLASSES = {"equity", "crypto"}


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
                "asset_class": (
                    ac
                    if (ac := str(item.get("asset_class", "")).strip().lower())
                    in _ASSET_CLASSES
                    else "equity"
                ),
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
    _write_json_atomic(SIGNALS_DIR / f"signals-{date_str}.json", snapshot)

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
def submit_batch(
    system: str, prompt_user: str, custom_id: str, *, web_search: bool = True
) -> str:
    """Submit a single-request Anthropic batch job. `web_search=False` drops the
    web_search tool (used for the weekly summary, which must not browse)."""
    params = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": prompt_user}],
    }
    if web_search:
        params["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    payload = {"requests": [{"custom_id": custom_id, "params": params}]}
    resp = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers=ANTHROPIC_HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    batch_id = resp.json()["id"]
    log.info(f"Batch submitted ({'search' if web_search else 'no-search'}): {batch_id}")
    return batch_id


def submit_batch_no_search(system: str, prompt_user: str, custom_id: str) -> str:
    """Submit a batch job without web search — used for the weekly summary."""
    return submit_batch(system, prompt_user, custom_id, web_search=False)


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
    text length, token usage + estimated batch cost, and tail). Only when the
    result looks anomalous — the model did
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
        usage = message.get("usage") or {}
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
        # Sonnet 4.6 Batch API: $1.50/M input, $7.50/M output, web_search $0.01/req.
        # Cache reads (~0.1x input) ignored here — estimate only, not a billing source.
        est_cost = in_tok / 1e6 * 1.50 + out_tok / 1e6 * 7.50 + searches * 0.01
        log.warning(
            "Batch result: stop_reason=%s blocks=%s text_len=%d "
            "usage(in=%s out=%s cache_read=%s searches=%s) est_batch_cost=$%.4f tail=%r",
            stop_reason,
            block_types,
            len(joined_text),
            in_tok,
            out_tok,
            cache_read,
            searches,
            est_cost,
            joined_text[-400:],
        )
        if stop_reason == "end_turn" and joined_text.strip():
            return  # healthy run — summary line is enough, skip the file dump
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dump_dir = DATA_DIR / "debug"
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
    return _load_json_or(STATE_FILE, None)


def save_state(updates: dict):
    # Lock the whole read-merge-write: a concurrent poller/submit must not
    # interleave between our load and write and lose the other's keys.
    with file_lock(STATE_FILE):
        state = load_state() or {}
        state.update(updates)
        _write_json_atomic(STATE_FILE, state, indent=None)


def clear_batch_state():
    with file_lock(STATE_FILE):
        state = load_state() or {}
        for key in ("batch_id", "submitted_at", "date", "weekly_batch_id"):
            state.pop(key, None)
        _write_json_atomic(STATE_FILE, state, indent=None)


# ── Delivery ──────────────────────────────────────────────────────────────────
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

    if ok:
        log.info(f"Delivered ({len(chunks)} msg(s))")
    else:
        log.error("Delivery had failures")
        telegram_alert(
            f"delivery had failed chunks for {archive_path.name} — content archived, check logs"
        )

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    plain = re.sub(r"<[^>]+>", "", text)
    archive_path.write_text(plain + "\n")
    log.info(f"Saved: {archive_path}")


# ── Modes ─────────────────────────────────────────────────────────────────────
def mode_submit():
    log.info("=== SUBMIT ===")
    # Commands are drained in real time by the long-running `commands` daemon, so
    # feedback.json is already current here. We must NOT poll getUpdates too: only
    # one getUpdates consumer is allowed per bot (a second one gets 409 Conflict).

    state = load_state() or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("date") == today and state.get("batch_id"):
        log.info(f"Already submitted today ({state['batch_id']}), skipping")
        return

    temp_sources = load_temp_sources()
    if temp_sources:
        log.info(
            f"Temp sources: {len(temp_sources)} ({', '.join(s['name'] for s in temp_sources)})"
        )
    feed_content = (
        "\n".join(c for f in RSS_FEEDS + temp_sources if (c := fetch_rss(f)))
        or "(no RSS content)"
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

    perf_block = performance_prompt_block(load_book())
    market_block = build_market_pulse(resolved_pins(fb))
    log.info(f"Market pulse: {len(market_block)} chars")
    enrichment_block = ""
    try:
        universe = build_universe(
            load_book(),
            load_watchlist(),
            latest_signal_tickers(SIGNALS_DIR),
            resolved_pins(fb),
        )
        bundles = build_enrichment(
            universe, as_of=datetime.now(timezone.utc).isoformat()
        )
        if not bundles.is_empty():
            _write_json_atomic(
                DATA_DIR / "enrichment" / f"enrichment-{today}.json", bundles.to_dict()
            )
            enrichment_block = render_prompt_block(bundles)
        log.info(
            "Enrichment: enabled=%s symbols=%d themes=%d block=%dch",
            enrichment_enabled(),
            len(bundles.symbols),
            len(bundles.themes),
            len(enrichment_block),
        )
    except Exception as e:
        log.error(f"Enrichment skipped (brief unaffected): {e}")
    prompt = build_daily_prompt(
        feed_content,
        web_content,
        chroma_context,
        yesterday_brief,
        weekly_summary,
        fb,
        portfolio,
        perf_block,
        market_block,
        enrichment_block,
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
        telegram_alert("collect found no pending batch — did last night's submit run?")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = poll_batch(batch_id)
    if raw:
        brief, raw_signals, status = split_brief_and_signals(raw)
        signals, dropped = normalize_signals(raw_signals)
        if dropped or status != "ok":
            log.warning(f"Signals: status={status}, dropped={dropped}")
        try:
            enr_path = DATA_DIR / "enrichment" / f"enrichment-{today}.json"
            if enr_path.exists():
                enr_raw = json.loads(enr_path.read_text(encoding="utf-8"))
                signals = annotate_signals(signals, bundles_from_dict(enr_raw))
        except Exception as e:
            log.error(f"Signal annotation skipped (signals unaffected): {e}")
        deliver(
            brief,
            header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
            archive_path=BRIEFS_DIR / f"brief-{today}.md",
        )
        save_signals(signals, today, status=status, dropped=dropped)
        clear_batch_state()
        # Trading stage runs AFTER clear_batch_state and is isolated: a matcher /
        # PolyGram / Claude failure must never re-collect and duplicate the brief.
        try:
            mode_paper()
            book = load_book()
            msg = daily_trade_message(book, today)
            if msg:
                telegram_send(msg)
        except Exception as e:
            log.error(f"Trading stage failed (brief already delivered): {e}")
            telegram_alert(f"trading stage failed after brief: {e}")
    else:
        log.error("Could not retrieve brief — will retry next collect run")
        telegram_alert(
            f"collect could not retrieve the brief (batch {batch_id}) — will retry next run"
        )


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
        telegram_alert("weekly summary batch failed or timed out")

    # Mark the paper book to market regardless of the weekly-summary outcome
    refresh_instruments_cache(force=True)
    with file_lock(trading.BOOK_FILE):
        book = mark_to_market(
            load_book(), datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        save_book(book)
    record_gate_history(book)
    telegram_send(performance_report(book))
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


# Bot command menu (Telegram autocomplete). Registered automatically by the
# daemon; keep in sync with the handlers in _handle_telegram_update and HELP_TEXT.
BOT_COMMANDS = [
    ("help", "Show command help"),
    ("status", "Show current overrides"),
    ("addsource", "Add a temporary news source (guided)"),
    ("sources", "List / remove temporary sources"),
    ("removesource", "Remove a temp source by name"),
    ("pin", "Always show a topic"),
    ("unpin", "Stop forcing a topic"),
    ("focus", "Emphasise something in upcoming briefs"),
    ("mute", "Reduce a quiet topic to one line"),
    ("note", "One-off instruction for the next brief"),
    ("dig", "Research a question now"),
    ("thesis", "Set a thesis for a ticker/cluster"),
    ("watch", "Track an instrument for volume alerts"),
    ("unwatch", "Stop watching an instrument"),
    ("positions", "Open positions with live marks"),
    ("performance", "Performance report + go-live gate"),
    ("close", "Close an open paper position"),
    ("reset", "Clear all overrides"),
]


def register_bot_commands_if_changed() -> None:
    """Push the command menu to Telegram via setMyCommands, but only when it has
    changed since last time (hash stored in state). New commands appear in the
    client's autocomplete automatically after a deploy — no manual BotFather step."""
    payload = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    h = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    if (load_state() or {}).get("cmd_hash") == h:
        return
    if telegram_set_my_commands(payload):
        save_state({"cmd_hash": h})
        log.info(f"Registered {len(payload)} bot commands")


def _handle_update(update: dict, fb: dict) -> dict:
    """Route one update to the message or callback handler. Returns the (possibly
    replaced) feedback dict — only message commands touch it; callbacks drive the
    separate wizard/temp-source state and leave fb unchanged."""
    if "callback_query" in update:
        return _handle_callback_query(update["callback_query"], fb)
    return _handle_telegram_update(update, fb)


def _drain_update_batch(updates: list, fb: dict, offset: int) -> tuple[dict, int]:
    """Apply one batch of updates and persist the advanced offset. Each update is
    handled in isolation and the offset always advances past it, so one malformed
    "poison" message fails without jamming the queue forever. The offset is saved
    via save_state's read-merge-write so a concurrent submit's batch_id survives.
    Factored out of the daemon loop to keep this logic unit-testable."""
    new_offset = offset
    for update in updates:
        new_offset = update.get("update_id", new_offset) + 1
        try:
            fb = _handle_update(update, fb)
            save_feedback(fb)
        except Exception as e:
            log.exception(f"Command failed (update {update.get('update_id')}): {e}")
            telegram_send("⚠️ Command failed — check logs.")
    if updates:
        save_state({"tg_offset": new_offset})
    return fb, new_offset


def mode_commands():
    """Long-running Telegram bot: real-time command + button handling via long
    polling. This is the ONLY getUpdates consumer (a second would 409), so it
    replaces the old per-cron drain — run it as a persistent service, not a cron.

    Feedback is held in memory and saved after each message; it has a single
    writer (this loop), so no cross-process lock is needed on it."""
    log.info("=== COMMANDS (daemon) ===")
    register_bot_commands_if_changed()
    offset = (load_state() or {}).get("tg_offset", 0)
    fb = load_feedback()
    while True:
        updates = telegram_get_updates(
            offset, timeout=30, allowed_updates=["message", "callback_query"]
        )
        if updates is None:  # transport/API error (incl. 409) — back off, retry
            time.sleep(5)
            continue
        fb, offset = _drain_update_batch(updates, fb, offset)


def mode_monitor():
    """Hourly cross-asset volume-anomaly alerts. Decoupled from the brief: its own
    cron mode, so a monitor failure can never delay or duplicate the morning brief."""
    log.info("=== MONITOR ===")
    alerts = run_volume_monitor()
    if alerts:
        telegram_send("🔔 <b>Volume alerts</b>\n\n" + "\n".join(alerts))


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    dispatch = {
        "submit": mode_submit,
        "collect": mode_collect,
        "weekly": mode_weekly,
        "run": mode_run,
        "commands": mode_commands,
        "paper": mode_paper,
        "monitor": mode_monitor,
    }
    fn = dispatch.get(mode)
    if fn:
        try:
            fn()
        except Exception as e:
            # Last-resort alert: without this, an uncaught crash is visible only
            # in the log file and the reader just silently gets no brief.
            log.exception(f"Mode '{mode}' crashed")
            telegram_alert(f"{mode} crashed: {type(e).__name__}: {e}")
            sys.exit(1)
    else:
        print("Usage: brief.py [submit|collect|weekly|run|commands|monitor]")
        sys.exit(1)
