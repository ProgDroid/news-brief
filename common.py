#!/usr/bin/env python3
"""Shared infrastructure for newsbrief: config, paths, logging, JSON I/O,
Telegram + HTML, Anthropic headers, and T212 auth. No domain logic; imported
by both brief.py and trading.py (one-way dependency, no cycles)."""

import base64
import os
import re
import json
import time
import logging
import requests
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager
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
        # Rotate so the log can't grow unbounded over the container's lifetime:
        # 5 MB × 5 backups ≈ 30 MB ceiling.
        handlers.append(
            RotatingFileHandler(
                DATA_DIR / "newsbrief.log", maxBytes=5_000_000, backupCount=5
            )
        )
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

# Default Sonnet model for the brief/trading synthesis calls. Override on the
# host with NEWSBRIEF_MODEL (single knob — also read by brief.SIGNALS_MODEL and
# claim_verify.VERIFY_MODEL) to swap models without a code change or redeploy.
# NOTE: brief.py declares the web_search_20260209 server tool, which requires
# Opus 4.6+ or Sonnet 4.6+. Setting this knob to Haiku 4.5 (or any older model)
# would 400 every call that browses — the tool version is a constraint on what
# this override may be set to.
MODEL = os.environ.get("NEWSBRIEF_MODEL", "claude-sonnet-5")

# ── Paths ─────────────────────────────────────────────────────────────────────
SIGNALS_DIR = DATA_DIR / "signals"
THESIS_LOG_FILE = DATA_DIR / "thesis_log.json"

# ── Trading212 config + auth ──────────────────────────────────────────────────
# Read-only API key. Base URL: live for real account, demo for practice.
# .strip() guards against a trailing newline/whitespace leaking in from .env or a
# Docker secret — T212 rejects a malformed Authorization value with 401.
T212_API_KEY_ID = os.environ.get("T212_API_KEY_ID", "").strip()
T212_API_KEY = os.environ.get("T212_API_KEY", "").strip()
T212_BASE_URL = os.environ.get("T212_BASE_URL", "https://live.trading212.com").strip()

# Alpaca market-data credentials (optional, like T212). Free signup gives a
# paper account with full Basic/IEX data access — no funding required.
ALPACA_API_KEY_ID = os.environ.get("APCA_API_KEY_ID", "").strip()
ALPACA_API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "").strip()
ALPACA_DATA_URL = os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets").strip()


# PolyGram (prediction markets) credentials — optional, like T212. Login is
# JWT-based; registration is manual/one-time and never in the cron path.
POLYGRAM_EMAIL = os.environ.get("POLYGRAM_EMAIL")
POLYGRAM_PASSWORD = os.environ.get("POLYGRAM_PASSWORD")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Live prediction trading (real money). Default OFF; funded/enabled per host.
PG_LIVE_ENABLED = _env_flag("PG_LIVE_ENABLED")
PG_LIVE_TOTAL_CAP = _env_float(
    "PG_LIVE_TOTAL_CAP", 50.0
)  # max USD across all live rows
PG_LIVE_PER_TRADE_CAP = _env_float("PG_LIVE_PER_TRADE_CAP", 5.0)  # max USD per order

# Sleeve A — systematic favorite-fade (real money). Default OFF.
PG_A_ENABLED = _env_flag("PG_A_ENABLED")
PG_A_STAKE = _env_float("PG_A_STAKE", 2.0)  # USD per fade
PG_A_BAND_LO = _env_float("PG_A_BAND_LO", 0.75)  # favorite-side entry band
PG_A_BAND_HI = _env_float("PG_A_BAND_HI", 0.92)
PG_A_SPREAD_GATE = _env_float(
    "PG_A_SPREAD_GATE", 0.03
)  # max half-spread (fraction of mid)
PG_A_TAKE = _env_float("PG_A_TAKE", 0.97)  # take-profit: held price repriced to ceiling
PG_A_STOP = _env_float(
    "PG_A_STOP", 0.15
)  # stop: absolute adverse drop from entry price
PG_A_TIME_STOP_DAYS = int(_env_float("PG_A_TIME_STOP_DAYS", 21))
PG_A_NEAR_DAYS = int(
    _env_float("PG_A_NEAR_DAYS", 10)
)  # ≤ this to settlement ⇒ hold, no time-stop

# ── Sleeve B: discretionary conviction holds ──────────────────────────────────
PG_B_ENABLED = _env_flag("PG_B_ENABLED")
PG_B_POS_CAP = _env_float(
    "PG_B_POS_CAP", 10.0
)  # per conviction bet (money-you-can-zero)
PG_B_TOTAL_CAP = _env_float("PG_B_TOTAL_CAP", 40.0)  # across all open Sleeve-B rows
PG_THESIS_GRACE_DAYS = int(_env_float("PG_THESIS_GRACE_DAYS", 14))

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


# ── Cross-process locking ─────────────────────────────────────────────────────
class LockTimeout(RuntimeError):
    """Raised when file_lock cannot acquire within its timeout."""


@contextmanager
def file_lock(
    path,
    *,
    timeout: float = 30.0,
    poll: float = 0.05,
    stale_after: float = 300.0,
):
    """Serialise a read-merge-write span across processes via an O_EXCL lock file.

    _write_json_atomic makes each write crash-safe, but two cron-driven
    containers that each load -> mutate -> save can still lose an update (e.g.
    `/close` writing the paper book while a collect run marks it to market). The
    lock makes those spans mutually exclusive.

    `path` may be the data file itself (the lock lives at "<path>.lock", the data
    file is never touched) or an explicit "*.lock" path. A lock left behind by a
    crashed holder is broken once it is older than `stale_after` so a dead process
    can't wedge the pipeline forever. Raises LockTimeout if the holder won't let
    go within `timeout`.
    """
    p = Path(path)
    lock = p if p.suffix == ".lock" else p.with_name(p.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    log.warning(f"Breaking stale lock {lock.name}")
                    os.unlink(lock)
                    continue
            except FileNotFoundError:
                continue  # released between our open and stat — retry at once
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not acquire {lock.name} within {timeout}s")
            time.sleep(poll)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass


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


# ── Sleeve-B conviction-thesis log ──────────────────────────────────────────────
def load_thesis_log() -> list:
    """The Sleeve-B conviction-thesis calibration corpus (list of records)."""
    data = _load_json_or(THESIS_LOG_FILE, [])
    return data if isinstance(data, list) else []


def save_thesis_log(records: list) -> None:
    _write_json_atomic(THESIS_LOG_FILE, records)


def append_thesis(record: dict) -> None:
    """Append one thesis record under the file lock (daemon + retention both touch it)."""
    with file_lock(THESIS_LOG_FILE):
        log_ = load_thesis_log()
        log_.append(record)
        save_thesis_log(log_)


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


def telegram_send_long(text: str) -> bool:
    """Send an HTML message, splitting it across multiple messages when it exceeds
    Telegram's length cap. Returns True only if every chunk sent.

    Use this instead of the bare telegram_send for any content that grows with the
    data (performance/positions reports, trade updates, alert lists) — a single
    telegram_send on such content 400s with "message is too long" once the book or
    watchlist gets big enough. Short messages send as one, unchanged.
    """
    chunks = split_html_message(text)
    ok = True
    for i, chunk in enumerate(chunks):
        if not telegram_send(chunk):
            log.error(f"telegram_send_long: chunk {i + 1}/{len(chunks)} failed")
            ok = False
        elif len(chunks) > 1:
            time.sleep(0.4)  # stay under Telegram's per-chat send rate
    return ok


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


# ── Telegram Bot API (interactive: buttons, callbacks, command menu) ───────────
# Used by the real-time commands daemon. Inline keyboards are JSON of the shape
# {"inline_keyboard": [[{"text": ..., "callback_data": ...}], ...]}; callback_data
# is capped at 64 bytes by Telegram, so callers key buttons by short ids.
def _tg_api(method: str, payload: dict, *, timeout: float = 15.0) -> dict | None:
    """POST one Bot API method; return the parsed JSON, or None on any failure.

    Errors are logged (token-redacted) and swallowed: a transient API hiccup must
    never crash the long-lived daemon, and a button action that fails just leaves
    the chat unchanged rather than taking the process down."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if not resp.ok:
            log.error(f"Telegram {method} {resp.status_code}: {resp.text[:300]}")
            return None
        return resp.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {_redact(str(e))}")
        return None


def telegram_send_buttons(text: str, inline_keyboard: list) -> int | None:
    """Send an HTML message carrying an inline keyboard; return its message_id
    (so the daemon can edit the same message through subsequent wizard steps)."""
    r = _tg_api(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": True,
            "reply_markup": {"inline_keyboard": inline_keyboard},
        },
    )
    return r["result"]["message_id"] if r and r.get("ok") else None


def telegram_edit_text(
    message_id: int, text: str, inline_keyboard: list | None = None
) -> None:
    """Edit an existing message's text (and optionally its keyboard). Passing an
    empty list for inline_keyboard strips the buttons; None leaves them as-is."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    _tg_api("editMessageText", payload)


def telegram_answer_callback(callback_id: str, text: str | None = None) -> None:
    """Acknowledge a callback query. Telegram shows a spinner on the tapped button
    until this is called, so every callback MUST be answered even with no text."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _tg_api("answerCallbackQuery", payload)


def telegram_set_my_commands(commands: list) -> bool:
    """Register the bot's command menu (the autocomplete list). `commands` is a
    list of {"command", "description"} dicts. Returns True on success."""
    r = _tg_api("setMyCommands", {"commands": commands})
    return bool(r and r.get("ok"))


# A surviving allowed tag (open/close, optional attrs) OR an existing HTML entity.
# Text NOT matched here is literal prose whose <, >, & must be escaped, else
# "oil <$60" / "AT&T" leave a bare < or & that 400s the whole Telegram chunk.
_PRESERVE_RE = re.compile(
    r"</?(?:" + "|".join(ALLOWED_TAGS) + r")(?:\s[^>]*)?>"
    r"|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);"
)


def _escape_bare_html(s: str) -> str:
    # & first, else the & in &lt;/&gt; we emit would itself get double-escaped.
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Telegram <a href> only honours web/Telegram links; a model (or an injected
# source) emitting javascript:/data:/file: etc. is a defang target.
_A_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE)
_SAFE_SCHEMES = {"http", "https", "tg", "mailto"}


def _defang_a_href(open_tag: str) -> str:
    """Rewrite an <a> open tag's href to '#' when its URL scheme isn't in the
    allowlist. Keeps the <a>...</a> PAIR intact (no orphaned </a>) — only the
    unsafe destination is neutralised."""

    def repl(m: "re.Match[str]") -> str:
        quote, url = m.group(1), m.group(2)
        scheme = re.match(r"\s*([a-zA-Z][a-zA-Z0-9+.\-]*):", url)
        if scheme and scheme.group(1).lower() not in _SAFE_SCHEMES:
            return f"href={quote}#{quote}"
        return m.group(0)

    return _HREF_RE.sub(repl, open_tag)


def sanitise_html(text: str) -> str:
    text = re.sub(r"```[a-z]*\n?", "", text)
    text = re.sub(
        r"</?(?!(?:" + "|".join(ALLOWED_TAGS) + r")(?:\s[^>]*)?>)[a-zA-Z][^>]*>",
        "",
        text,
    )
    # Whitelist-preserving escape: leave the surviving allowed tags and existing
    # entities verbatim, escape stray <, >, & in the literal text between them.
    out, pos = [], 0
    for m in _PRESERVE_RE.finditer(text):
        out.append(_escape_bare_html(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_bare_html(text[pos:]))
    text = "".join(out)
    # Surviving <a> tags are now verbatim; defang any unsafe href scheme.
    text = _A_TAG_RE.sub(lambda m: _defang_a_href(m.group(0)), text)
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
            # Do not .strip() the hard-sliced piece: if the boundary lands on
            # whitespace, stripping the slice while advancing `part` by the raw
            # max_len would silently drop that whitespace, merging the words on
            # either side of the seam (e.g. "hello world" -> "helloworld").
            chunks.append(part[:max_len])
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
