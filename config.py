#!/usr/bin/env python3
"""Identity and configuration resolved from Postgres, not the environment.

The environment keeps what belongs to the *deployment* — credentials and the
database connection. Everything that describes *this reader* is a row, which is
what retires the `env-var-needs-compose-passthrough` bug class: a knob set on
the host or in `.env` but never declared in the `&newsbrief` compose anchor is
invisible inside the container, and a fail-closed flag then no-ops in silence.
A row cannot be invisible that way (spec section 6.3).

`db` imports `common`, so `common` cannot import this module at the top level
without closing a cycle. The senders in `common.py` import it inside the
function instead — one place, one comment, rather than four.
"""

import os
import threading
import time
from typing import NamedTuple

import db
from common import log

# How long a resolved value is trusted. Sized for the resident commands child,
# which never restarts: success criterion 8 says configuration changes take
# effect without recreating a container, and a value bound at import would make
# that false. A minute is short enough that a change lands while the operator is
# still looking at Telegram, and long enough that the daemon's hot loop is not
# one query per message.
TTL_SECONDS = 60.0

# After a failed refresh the surviving value is trusted for this long rather than
# a full TTL — shorter on purpose. An outage should cost about one attempted
# lookup every ten seconds rather than one per call, and the real value should
# come back promptly once Postgres does rather than a minute later.
STALE_RETRY_SECONDS = 10.0


class _Entry(NamedTuple):
    """A cached value, with when it was read and whether it is a stale survivor.

    It stores the READ TIME rather than an expiry, so freshness is computed
    against the current `TTL_SECONDS` on every check. Baking the expiry in at
    store time would freeze the window an entry was written under — which makes
    the constant untestable, and would silently ignore a TTL that becomes
    runtime-tunable when the knobs themselves move into `settings`.
    """

    read_at: float
    value: object
    stale: bool


_lock = threading.Lock()
_cache: dict[str, _Entry] = {}

_USER_COLUMNS = (
    "id",
    "display_name",
    "telegram_chat_id",
    "timezone",
    "delivery_time",
    "active",
)
_USER_SELECT = ", ".join(_USER_COLUMNS)

# The lock name the seed is serialised on. A plain SELECT-then-INSERT races
# between the supervisor's startup and a `docker compose run --rm` issued in the
# same second, and the loser inserts a second operator.
_SEED_LOCK = "seed_users"


def invalidate() -> None:
    """Drop every cached value. Called after a write, and by tests."""
    with _lock:
        _cache.clear()


def _cached(key: str, produce):
    """Read through the cache, serving the last known value if a refresh fails.

    The stale path exists because identity sits on the command-auth path. If a
    Postgres blip made `chat_id` raise, the bot would go deaf — and the bot is
    the channel the operator would use to find out Postgres is unwell. That is
    the same fail-open reasoning as spec section 8, applied to identity rather
    than to migrations.

    It is not a fallback. With nothing cached there is nothing honest to serve,
    so a cold failure propagates: inventing a value there is exactly how a
    misconfigured container comes up looking healthy.
    """
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
    if entry is not None:
        ttl = STALE_RETRY_SECONDS if entry.stale else TTL_SECONDS
        if now - entry.read_at < ttl:
            return entry.value
    try:
        value = produce()
    except Exception:
        if entry is None:
            raise
        log.exception(
            f"Config refresh for '{key}' failed; serving the last known value"
        )
        with _lock:
            _cache[key] = _Entry(now, entry.value, stale=True)
        return entry.value
    with _lock:
        _cache[key] = _Entry(now, value, stale=False)
    return value


def _read_active_user() -> dict:
    """The single-user resolution point, and the place multi-user starts.

    `LIMIT 1` is the honest shape of today's system rather than an oversight:
    delivery, auth and preferences all assume one reader. A second row is a
    product decision (spec section 6.4), and when it arrives this function is
    what gets replaced by a per-delivery loop — the callers already ask for "the
    user", so the blast radius is here.
    """
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT {_USER_SELECT} FROM users WHERE active ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "No active user row: the database is migrated but unseeded, or every "
            "user is inactive. `ensure_seeded` runs at supervisor startup and "
            "before any mode dispatch; check the log for why it did not."
        )
    return dict(zip(_USER_COLUMNS, row))


def active_user() -> dict:
    """The reader this deployment serves: id, name, chat id, timezone, delivery."""
    return _cached("active_user", _read_active_user)


def chat_id() -> str:
    """The Telegram delivery target, and the only chat allowed to drive the bot.

    No environment fallback, deliberately. A default here would deliver someone
    else's brief to whoever the environment last named, and would make a
    half-configured deployment behave like a working one.
    """
    return str(active_user()["telegram_chat_id"])


def alert_chat_id() -> str:
    """Where operational alerts go — `chat_id`, with the environment as a last
    resort.

    The single exception to hard-requiring the database, and narrow on purpose:
    this is the channel that reports the database being unreachable, so it must
    not be able to fail for the reason it is reporting. Every other read raises.
    """
    try:
        return chat_id()
    except Exception:
        return os.environ.get("TELEGRAM_CHAT_ID", "")


def ensure_seeded(conn) -> bool:
    """Create the operator row from `TELEGRAM_CHAT_ID` when `users` is empty.

    Not a migration: a `.sql` file cannot read the environment. It runs from two
    entry points because both are real — the supervisor after migrations, and
    `brief.py`'s dispatch, since `docker compose run --rm newsbrief collect`
    bypasses the supervisor entirely. That is the section 4.4a lesson (a second
    entry path is not a hypothetical) applied to seeding.

    Returns whether it inserted, so the caller can log a first boot distinctly
    from every boot after it.
    """
    with db.advisory_lock(conn, _SEED_LOCK) as got:
        if not got:
            # Another entry path is seeding right now. It will win; this process
            # reads the row it writes.
            return False
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return False
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat:
            raise RuntimeError(
                "Cannot seed the operator: TELEGRAM_CHAT_ID is unset. It is "
                "bootstrap input only — read once, when the users table is "
                "empty — but a first boot has no other way to learn who the "
                "operator is, so it must be declared in the &newsbrief anchor."
            )
        conn.execute(
            "INSERT INTO users (display_name, telegram_chat_id) VALUES (%s, %s)",
            ("operator", chat),
        )
        conn.commit()
    invalidate()
    log.info("Seeded the operator user from TELEGRAM_CHAT_ID")
    return True
