"""How the connection string is assembled, and what happens when it cannot be.

These tests need no database: the defect they pin is in the string, not in the
connection. Compose used to splice POSTGRES_PASSWORD into a `postgresql://` URI
by plain substitution with no encoding, so a password containing `/`, `%` or `@`
produced a URI that libpq parsed into a different database — or refused. The
whole point of building keyword/value conninfo instead is that the password is
carried as a VALUE and never has to survive a URI parse.
"""

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import db

_VARS = (
    "DATABASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
)


@pytest.fixture()
def env(monkeypatch):
    """Start from no database configuration at all.

    CI exports DATABASE_URL for the DB-backed suites, so an unset variable here
    has to be made unset rather than assumed.
    """
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _discrete(env, password=None):
    env.setenv("POSTGRES_HOST", "postgres")
    env.setenv("POSTGRES_USER", "newsbrief")
    env.setenv("POSTGRES_PASSWORD", password or "secret")
    env.setenv("POSTGRES_DB", "newsbrief")


def test_database_url_wins_and_is_used_verbatim(env):
    _discrete(env)
    env.setenv("DATABASE_URL", "postgresql://other@elsewhere:5432/other")

    assert db.conninfo() == "postgresql://other@elsewhere:5432/other"


def test_discrete_variables_build_a_conninfo(env):
    _discrete(env)

    assert conninfo_to_dict(db.conninfo()) == {
        "host": "postgres",
        "user": "newsbrief",
        "password": "secret",
        "dbname": "newsbrief",
    }


@pytest.mark.parametrize(
    "password",
    [
        "a/b",  # libpq read what followed as the port
        "a%zz",  # invalid percent-encoded token
        "a%cd",  # decoded SILENTLY to a different password
        "a@b",  # truncated the password and corrupted the host
        "a b'c\"d\\e",  # never reachable through a URI at all
    ],
)
def test_password_survives_characters_that_broke_the_uri(env, password):
    _discrete(env, password=password)

    assert conninfo_to_dict(db.conninfo())["password"] == password


def test_port_is_carried_when_set(env):
    _discrete(env)
    env.setenv("POSTGRES_PORT", "5433")

    assert conninfo_to_dict(db.conninfo())["port"] == "5433"


def test_missing_variables_are_named_one_by_one(env):
    _discrete(env)
    env.delenv("POSTGRES_PASSWORD")

    with pytest.raises(RuntimeError) as exc:
        db.conninfo()

    assert "POSTGRES_PASSWORD" in str(exc.value)
    assert "POSTGRES_HOST" not in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)


def test_is_configured_answers_for_the_discrete_path(env):
    assert db.is_configured() is False
    _discrete(env)
    assert db.is_configured() is True


def test_is_configured_answers_for_the_url_path(env):
    env.setenv("DATABASE_URL", "postgresql://nb@postgres:5432/nb")

    assert db.is_configured() is True


def test_connect_hands_libpq_what_conninfo_built(env, monkeypatch):
    """The skip guards read `is_configured`; `connect` must read the same string."""
    _discrete(env, password="a/b@c")
    seen = {}

    def fake_connect(conninfo, **kwargs):
        seen["conninfo"] = conninfo
        seen["kwargs"] = kwargs
        return "connection"

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    assert db.connect(connect_timeout=3) == "connection"
    assert conninfo_to_dict(seen["conninfo"])["password"] == "a/b@c"
    assert seen["kwargs"]["connect_timeout"] == 3
