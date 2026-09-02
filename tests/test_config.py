"""Identity resolution: the operator seed, the cache, and what happens when
Postgres is unreachable.

DB-backed for the same reason `test_db.py` is: the whole point of `config` is
that configuration comes from rows, and a fake would test the fake. They skip
loudly when no database is configured, never silently.

The cache tests are the ones worth reading. `chat_id` is on the command-auth
path, so its failure modes are the interesting behaviour: a cold miss must
raise rather than default to something plausible, and a warm miss must serve
the last known value rather than take the control channel down with the
database.
"""

import pytest

import common
import config
import db

pytestmark = [
    pytest.mark.skipif(
        not db.is_configured(),
        reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
        "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
        "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
    ),
    # Opts this module out of conftest's suite-wide `config.chat_id` stub.
    # Everywhere else that stub is what keeps the suite infra-free; here it
    # would replace the function under test with a constant, and these tests
    # would pass against no implementation at all.
    pytest.mark.real_config,
]


@pytest.fixture()
def conn():
    """A migrated database with no users row: the first-boot state."""
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        config.invalidate()
        yield c


def _users(conn) -> list[tuple]:
    return conn.execute(
        "SELECT display_name, telegram_chat_id, active FROM users ORDER BY id"
    ).fetchall()


# ── Seeding ──────────────────────────────────────────────────────────────────


def test_seeds_one_operator_row_from_the_environment(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    assert config.ensure_seeded(conn) is True
    assert _users(conn) == [("operator", "555", True)]


def test_seeding_twice_inserts_one_row(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    assert config.ensure_seeded(conn) is False
    assert len(_users(conn)) == 1


def test_the_environment_is_ignored_once_a_row_exists(conn, monkeypatch):
    """This is the point of the phase: TELEGRAM_CHAT_ID is bootstrap input, not
    runtime configuration. A changed env must not move the delivery target."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    config.ensure_seeded(conn)
    assert _users(conn) == [("operator", "555", True)]


def test_refuses_to_seed_a_row_with_no_chat_id(conn, monkeypatch):
    """A users row with an empty chat id is worse than no row: every send would
    200 into nowhere and the failure would be invisible."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        config.ensure_seeded(conn)
    assert _users(conn) == []


# ── Reading ──────────────────────────────────────────────────────────────────


def test_chat_id_comes_from_the_seeded_row(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    assert config.chat_id() == "555"


def test_active_user_carries_the_delivery_fields(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    user = config.active_user()
    assert user["telegram_chat_id"] == "555"
    assert user["timezone"] == "UTC"
    assert user["id"] > 0


def test_inactive_users_are_not_resolved(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    conn.execute("UPDATE users SET active = FALSE")
    conn.commit()
    config.invalidate()
    with pytest.raises(RuntimeError, match="active"):
        config.chat_id()


def test_an_unseeded_database_raises_rather_than_guessing(conn, monkeypatch):
    """Hard-require: no env fallback on the ordinary read path. A default here
    would send someone else's brief to whoever the environment last named."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    with pytest.raises(RuntimeError):
        config.chat_id()


# ── The cache, and Postgres going away ───────────────────────────────────────


def test_a_changed_row_is_picked_up_once_the_ttl_expires(conn, monkeypatch):
    """Success criterion 8: configuration changes take effect without recreating
    a container. The resident commands child never restarts, so a value frozen
    at import would make that criterion false."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    assert config.chat_id() == "555"

    conn.execute("UPDATE users SET telegram_chat_id = '777'")
    conn.commit()
    assert config.chat_id() == "555", "inside the TTL the cached value stands"

    monkeypatch.setattr(config, "TTL_SECONDS", 0)
    assert config.chat_id() == "777"


def test_a_failed_refresh_serves_the_last_known_value(conn, monkeypatch):
    """A Postgres blip must not cost the operator the control channel: the bot
    is the thing that would report the blip. This is the fail-open half of
    spec section 8, applied to identity."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    assert config.chat_id() == "555"

    monkeypatch.setattr(config, "TTL_SECONDS", 0)
    monkeypatch.setattr(config, "_read_active_user", _boom)
    assert config.chat_id() == "555"


def test_a_cold_failure_still_raises(monkeypatch):
    """Stale-serve is not a fallback. With nothing cached there is nothing
    honest to serve, and inventing one is how a misconfigured container looks
    healthy."""
    config.invalidate()
    monkeypatch.setattr(config, "_read_active_user", _boom)
    with pytest.raises(RuntimeError):
        config.chat_id()


def test_alert_chat_id_falls_back_to_the_environment(monkeypatch):
    """The alert path is the exception, deliberately. It is the channel that
    reports the database being down, so it must not depend on the database.
    Narrow on purpose: `chat_id` has no such fallback."""
    config.invalidate()
    monkeypatch.setattr(config, "_read_active_user", _boom)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    assert config.alert_chat_id() == "555"


def _boom():
    raise RuntimeError("postgres is gone")


# ── Settings: the importer and the knob read path ────────────────────────────


def _settings(conn) -> dict:
    return {
        k: v
        for k, v in conn.execute(
            "SELECT key, value FROM settings WHERE user_id IS NULL"
        ).fetchall()
    }


def test_imports_only_the_knobs_the_environment_actually_sets(conn, monkeypatch):
    """Writing every knob would freeze today's defaults into rows, and a later
    change to a default in code would then be overridden by a row nobody chose."""
    monkeypatch.setenv("PG_A_ENABLED", "1")
    monkeypatch.setenv("VOL_SPIKE_MULT", "3.5")
    monkeypatch.delenv("PG_B_ENABLED", raising=False)

    imported = config.import_settings_from_env(conn)

    assert set(imported) >= {"PG_A_ENABLED", "VOL_SPIKE_MULT"}
    assert "PG_B_ENABLED" not in _settings(conn)


def test_the_importer_runs_only_while_settings_is_empty(conn, monkeypatch):
    """Emptiness is the idempotence guard, which is what makes rollback mean
    'keep the compose anchor' rather than 'restore a backup'."""
    monkeypatch.setenv("PG_A_ENABLED", "1")
    config.import_settings_from_env(conn)

    monkeypatch.setenv("PG_A_STAKE", "9.0")
    assert config.import_settings_from_env(conn) == []
    assert "PG_A_STAKE" not in _settings(conn)


def test_a_knob_reads_from_its_row(conn, monkeypatch):
    monkeypatch.setenv("PG_A_STAKE", "7.5")
    config.import_settings_from_env(conn)
    assert config.knob("PG_A_STAKE") == 7.5


def test_a_knob_with_no_row_is_its_default_not_the_environment(conn, monkeypatch):
    """The hard-require decision, stated as a test. If an absent row fell back to
    os.environ, a knob set on the host but never declared in the compose anchor
    would go on being silently invisible — the exact bug this phase retires,
    only now with a database making it look deliberate."""
    config.import_settings_from_env(conn)  # empty environment: imports nothing
    monkeypatch.setenv("PG_A_STAKE", "99.0")
    config.invalidate()
    assert config.knob("PG_A_STAKE") == common.KNOBS["PG_A_STAKE"].default


def test_the_env_name_is_the_stored_key_where_they_differ(conn, monkeypatch):
    """MODEL has always been set with NEWSBRIEF_MODEL. The operator-facing name
    is what goes in the row; renaming it is not this change's business."""
    monkeypatch.setenv("NEWSBRIEF_MODEL", "claude-opus-5")
    config.import_settings_from_env(conn)
    assert _settings(conn)["NEWSBRIEF_MODEL"] == "claude-opus-5"
    assert config.knob("MODEL") == "claude-opus-5"


def test_a_changed_setting_lands_without_a_restart(conn, monkeypatch):
    """Success criterion 8, for knobs rather than identity."""
    monkeypatch.setenv("PG_A_STAKE", "2.0")
    config.import_settings_from_env(conn)
    assert config.knob("PG_A_STAKE") == 2.0

    conn.execute("UPDATE settings SET value = '4.0' WHERE key = 'PG_A_STAKE'")
    conn.commit()
    monkeypatch.setattr(config, "TTL_SECONDS", 0)
    assert config.knob("PG_A_STAKE") == 4.0


def test_per_user_rows_are_not_read_as_global(conn, monkeypatch):
    """`settings` is scoped, and the global reader must stay global: a per-user
    row leaking into it would apply one reader's preference to every job."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    config.ensure_seeded(conn)
    user_id = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute(
        "INSERT INTO settings (key, user_id, value) VALUES ('PG_A_STAKE', %s, '99')",
        (user_id,),
    )
    conn.commit()
    config.invalidate()
    assert config.knob("PG_A_STAKE") == common.KNOBS["PG_A_STAKE"].default


def test_the_documented_upsert_command_works(conn, monkeypatch):
    """The README tells the operator to change a knob with this exact statement.

    `ON CONFLICT (key) WHERE user_id IS NULL` infers the partial unique index
    from migration 0001, and a partial index needs that predicate spelled out —
    without it Postgres cannot tell which of the two indexes is meant and the
    command fails. Pinned here because documentation that has never been run is
    a belief, and this one is the answer to "how do I turn a sleeve on".
    """
    upsert = (
        "INSERT INTO settings (key, user_id, value) VALUES ('PG_A_ENABLED', NULL, %s) "
        "ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value = EXCLUDED.value"
    )
    conn.execute(upsert, ("1",))
    conn.execute(upsert, ("0",))
    conn.commit()

    assert _settings(conn) == {"PG_A_ENABLED": "0"}
    config.invalidate()
    assert config.knob("PG_A_ENABLED") is False
