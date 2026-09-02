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

import json
import os
import threading
import time
from typing import NamedTuple

import common
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

# Its counterpart for the settings importer, for the same reason: the supervisor
# and a `docker compose run --rm` can start in the same second.
_SETTINGS_LOCK = "import_settings"
_SOURCES_LOCK = "import_sources"

# The file the source importer drains, once. It is the pre-phase-2 store and is
# deliberately left on disk after the import: keeping it IS the rollback.
LEGACY_SOURCES_FILE = common.DATA_DIR / "sources.json"


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


# ── Knobs ────────────────────────────────────────────────────────────────────


def _read_settings() -> dict[str, str]:
    """Every global setting, in one query.

    One query per TTL for all of them rather than one per knob: a single brief
    reads a couple of dozen, and `settings` is a table of tens of rows, so
    fetching the lot is cheaper than being clever about it.

    Global scope only (`user_id IS NULL`). Per-user knobs are what the
    `preferences` table is for; a knob that becomes per-user gets a reader that
    knows whose it is, rather than this one quietly changing meaning.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id IS NULL"
        ).fetchall()
    return {key: value for key, value in rows}


def settings() -> dict[str, str]:
    """The cached global settings map, refreshed on the same terms as identity."""
    return _cached("settings", _read_settings)


def knob(name: str):
    """Resolve one knob by its `common.KNOBS` name. Called by common.__getattr__.

    An absent row means the code default, NOT a lookup in the environment. That
    is the hard-require decision: the importer reads the environment exactly
    once, on first boot, and after that a knob's value is a row or a default.
    Falling back to the environment at read time would recreate the bug this
    phase exists to retire — a knob set on the host, invisible in the container,
    silently no-op — only now with a database making it look deliberate.
    """
    spec = common.KNOBS[name]
    raw = settings().get(spec.key(name))
    return spec.default if raw is None else common.coerce_knob(spec, raw)


def import_settings_from_env(conn) -> list[str]:
    """Copy knobs set in the environment into `settings`, once, when it is empty.

    Emptiness is the idempotence guard (spec section 7.3), which is also what
    makes rollback cheap: the compose anchor still carries the variables, so
    reverting the code is enough — there is nothing to restore.

    Only knobs actually PRESENT in the environment are written. Writing every
    knob would freeze today's defaults into rows, and a later change to a
    default in code would then be silently overridden by a row nobody chose.
    """
    with db.advisory_lock(conn, _SETTINGS_LOCK) as got:
        if not got:
            return []
        if conn.execute(
            "SELECT 1 FROM settings WHERE user_id IS NULL LIMIT 1"
        ).fetchone():
            return []
        imported = []
        for name, spec in common.KNOBS.items():
            key = spec.key(name)
            raw = os.environ.get(key)
            if raw is None or not raw.strip():
                continue
            conn.execute(
                "INSERT INTO settings (key, user_id, value) VALUES (%s, NULL, %s)",
                (key, raw.strip()),
            )
            imported.append(key)
        conn.commit()
    invalidate()
    if imported:
        log.info(
            f"Imported {len(imported)} settings from the environment: "
            f"{', '.join(sorted(imported))}"
        )
    return imported


# ── Sources ──────────────────────────────────────────────────────────────────

_SOURCE_COLUMNS = (
    "name",
    "url",
    "category",
    "kind",
    "source_type",
    "perspective",
    "state_funded",
)
_SOURCE_SELECT = ", ".join(_SOURCE_COLUMNS)


def _read_sources() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_SOURCE_SELECT} FROM sources WHERE user_id = %s ORDER BY id",
            (active_user()["id"],),
        ).fetchall()
    return [dict(zip(_SOURCE_COLUMNS, row)) for row in rows]


def sources() -> list[dict]:
    """The active user's Telegram-managed sources, cached like everything else.

    Cached because a submit calls this several times — once to build the fetch
    list, once to index kinds and perspectives for attribution — and because the
    resident daemon must see an /addsource land without a restart.
    """
    return _cached("sources", _read_sources)


def add_source(entry: dict) -> None:
    """Insert or replace a source, deduped on URL by the unique index.

    `add_temp_source` used to read the whole file, filter out a matching URL,
    append, and write it back under a lock. The upsert is that rule expressed
    where it belongs — in the store — and it cannot lose a concurrent write the
    way a read-modify-write can.
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sources (user_id, name, url, category, kind, source_type, "
            "perspective, state_funded) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, url) DO UPDATE SET "
            "name = EXCLUDED.name, category = EXCLUDED.category, "
            "kind = EXCLUDED.kind, source_type = EXCLUDED.source_type, "
            "perspective = EXCLUDED.perspective, "
            "state_funded = EXCLUDED.state_funded",
            (
                active_user()["id"],
                entry["name"],
                entry["url"],
                entry["category"],
                entry.get("kind", "regional"),
                entry.get("source_type", "feed"),
                entry.get("perspective"),
                bool(entry.get("state_funded", False)),
            ),
        )
        conn.commit()
    invalidate()


def delete_source(url: str) -> dict | None:
    """Delete by URL; return the deleted row, or None if it was not there.

    DELETE ... RETURNING is atomic, so the read-modify-write race the file lock
    existed to prevent is simply gone: two concurrent removals of the same
    source cannot both claim to have removed it.
    """
    with db.connect() as conn:
        row = conn.execute(
            f"DELETE FROM sources WHERE user_id = %s AND url = %s "
            f"RETURNING {_SOURCE_SELECT}",
            (active_user()["id"], url),
        ).fetchone()
        conn.commit()
    invalidate()
    return dict(zip(_SOURCE_COLUMNS, row)) if row else None


def import_sources_from_file(conn, path=None) -> int:
    """Copy sources.json into the table, once, while it is empty.

    Same emptiness guard as the settings importer and for the same reason: it
    makes the import idempotent, so rollback means keeping the file rather than
    restoring a backup.

    A malformed file imports nothing and logs — it must not be able to stop a
    boot. The rows it writes are validated by the schema, and anything the
    schema rejects is skipped individually, so one bad hand-edited entry cannot
    cost the operator the other twenty.
    """
    path = path or LEGACY_SOURCES_FILE
    with db.advisory_lock(conn, _SOURCES_LOCK) as got:
        if not got:
            return 0
        if conn.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, json.JSONDecodeError):
            log.exception(f"Could not read {path}; importing no sources")
            return 0
        if not isinstance(raw, list):
            log.warning(f"{path} is not a list; importing no sources")
            return 0
        user_id = conn.execute(
            "SELECT id FROM users WHERE active ORDER BY id LIMIT 1"
        ).fetchone()
        if user_id is None:
            log.warning("No active user; importing no sources")
            return 0
        imported = 0
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO sources (user_id, name, url, category, kind, "
                        "source_type, perspective, state_funded) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id, url) DO NOTHING",
                        (
                            user_id[0],
                            entry["name"],
                            entry["url"],
                            str(entry["category"]).lower(),
                            entry.get("kind", "regional"),
                            entry.get("source_type", "feed"),
                            entry.get("perspective"),
                            bool(entry.get("state_funded", False)),
                        ),
                    )
                imported += 1
            except Exception:
                log.exception(f"Skipped an unimportable source: {entry!r}")
        conn.commit()
    invalidate()
    if imported:
        log.info(f"Imported {imported} sources from {path}")
    return imported


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
