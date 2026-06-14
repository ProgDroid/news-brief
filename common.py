#!/usr/bin/env python3
"""Shared infrastructure for newsbrief: config, paths, logging, JSON I/O,
Telegram + HTML, Anthropic headers, and T212 auth. No domain logic; imported
by both brief.py and trading.py (one-way dependency, no cycles)."""

import base64
import os
import re
import json
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
# Required at runtime — validated in __main__ before dispatch. Read with .get()
# so the module stays importable (for tests, tooling) without a full environment.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

REQUIRED_ENV = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# Root for all persistent state, archives and the log file. /app/logs is the
# container volume mount; override NEWSBRIEF_DATA_DIR for local runs and tests.
DATA_DIR = Path(os.environ.get("NEWSBRIEF_DATA_DIR", "/app/logs"))


# ── Logging ───────────────────────────────────────────────────────────────────
def _log_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(DATA_DIR / "newsbrief.log"))
    except OSError:
        pass  # data dir unavailable (local run, tests): console logging still works
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_log_handlers(),
)
log = logging.getLogger("newsbrief")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

MODEL = "claude-sonnet-4-6"

# ── Paths ─────────────────────────────────────────────────────────────────────
SIGNALS_DIR = DATA_DIR / "signals"

# ── Trading212 config + auth ──────────────────────────────────────────────────
# Read-only API key. Base URL: live for real account, demo for practice.
# .strip() guards against a trailing newline/whitespace leaking in from .env or a
# Docker secret — T212 rejects a malformed Authorization value with 401.
T212_API_KEY_ID = os.environ.get("T212_API_KEY_ID", "").strip()
T212_API_KEY = os.environ.get("T212_API_KEY", "").strip()
T212_BASE_URL = os.environ.get("T212_BASE_URL", "https://live.trading212.com").strip()


# PolyGram (prediction markets) credentials — optional, like T212. Login is
# JWT-based; registration is manual/one-time and never in the cron path.
POLYGRAM_EMAIL = os.environ.get("POLYGRAM_EMAIL")
POLYGRAM_PASSWORD = os.environ.get("POLYGRAM_PASSWORD")

# ── Phase 4: validation / performance ─────────────────────────────────────────
# Round-trip cost haircut (basis points) applied to gross return at close, by asset
# class. Prediction uses the real orderbook half-spread when available (see trading
# ._fetch_pg_half_spread); this is the fallback / momentum-exit cost.
HAIRCUT_BPS_EQUITY = int(os.environ.get("HAIRCUT_BPS_EQUITY", "10"))
HAIRCUT_BPS_CRYPTO = int(os.environ.get("HAIRCUT_BPS_CRYPTO", "26"))
HAIRCUT_BPS_PREDICTION = int(os.environ.get("HAIRCUT_BPS_PREDICTION", "200"))
# Go-live readiness gate (per asset class). Informational — nothing auto-enables live.
GATE_MIN_TRADES = int(os.environ.get("GATE_MIN_TRADES", "30"))
GATE_MIN_HIT_RATE = float(os.environ.get("GATE_MIN_HIT_RATE", "0.55"))
GATE_SUSTAINED_EVALS = int(os.environ.get("GATE_SUSTAINED_EVALS", "2"))

# ── Volume monitor (Phase 5) ──────────────────────────────────────────────────
# Anomaly = current-period volume / trailing-mean >= VOL_SPIKE_MULT, gated by an
# optional absolute per-asset floor and a minimum trailing-sample warm-up. A
# per-instrument cooldown suppresses re-alerting (daily equity volume fires once;
# intraday crypto can re-fire after the window).
VOL_SPIKE_MULT = float(os.environ.get("VOL_SPIKE_MULT", "2.5"))
VOL_TRAILING_N = int(os.environ.get("VOL_TRAILING_N", "20"))
VOL_MIN_SAMPLES = int(os.environ.get("VOL_MIN_SAMPLES", "5"))
VOL_ALERT_COOLDOWN_HRS = float(os.environ.get("VOL_ALERT_COOLDOWN_HRS", "12"))
VOL_FLOOR_EQUITY = float(os.environ.get("VOL_FLOOR_EQUITY", "0"))
VOL_FLOOR_CRYPTO = float(os.environ.get("VOL_FLOOR_CRYPTO", "0"))
VOL_FLOOR_PREDICTION = float(os.environ.get("VOL_FLOOR_PREDICTION", "0"))


def t212_auth_header() -> str:
    """Build the T212 HTTP Basic Authorization value from the configured credentials.

    base64.b64encode never inserts line breaks (unlike GNU `base64` without -w0, or
    the legacy base64.encodebytes), so the encoded value is always header-safe.
    """
    token = base64.b64encode(
        f"{T212_API_KEY_ID}:{T212_API_KEY}".encode("utf-8")
    ).decode("utf-8")
    return f"Basic {token}"


# ── JSON persistence ──────────────────────────────────────────────────────────
def _write_json_atomic(path: Path, data, indent: int | None = 2) -> None:
    """Write JSON via temp file + os.replace so a crash mid-write can never
    leave a truncated file behind (os.replace is atomic on POSIX and NTFS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def _load_json_or(path: Path, default):
    """Load JSON, quarantining a corrupt/unreadable file instead of crashing.

    One bad write (crash mid-write, full disk) must not wedge every mode at
    startup — losing one file of state beats a pipeline that can't start. The
    corrupt file is renamed aside (*.corrupt-<ts>) for post-mortem, not deleted.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, quarantine)
            log.error(f"Corrupt JSON {path.name} quarantined as {quarantine.name}: {e}")
        except OSError as qe:
            log.error(f"Corrupt JSON {path.name} (quarantine failed: {qe}): {e}")
        return default


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_MAX_LEN = 4000
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}


def _redact(text: str) -> str:
    """Strip the bot token from text destined for logs or alerts.

    requests exceptions embed the full request URL — which for the Telegram API
    contains the bot token — so raw exception text must never be logged as-is.
    """
    return (
        text.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***") if TELEGRAM_BOT_TOKEN else text
    )


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
        log.error(f"Telegram failed: {_redact(str(e))}")
        return False


def telegram_alert(text: str) -> None:
    """Send an operational failure alert to the reader.

    Failures used to be log-file-only, so the system could announce success but
    never its own breakage — "no brief arrived" was the only symptom. This is
    deliberately plain text (no parse_mode): arbitrary exception text containing
    '<' or '&' must never be able to 400 the message that reports failures.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"🚨 newsbrief: {_redact(text)[:3500]}",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        log.error(f"Alert send failed: {_redact(str(e))}")


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
        # A single paragraph longer than a whole message can't be placed by the
        # paragraph-boundary logic — hard-slice it, else the oversized chunk
        # would exceed Telegram's limit and the API would reject the message.
        while len(part) > max_len:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(part[:max_len].strip())
            part = part[max_len:]
        if len(current) + len(part) > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks
