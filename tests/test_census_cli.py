"""`brief.mode_census_prepare`'s CLI-level guards.

Every refusal path here must exit 2 before ever reaching `census.prepare`'s
own logic (that logic is covered by tests/test_census_prepare.py). Three of
the five paths below never touch the database at all (the env checks return
before `db.connect()`); the module is gated on a real database anyway for
consistency with the rest of the census suite, and because the last two
paths genuinely need one.
"""

import contextlib

import pytest

import brief
import census
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


def test_missing_env_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CENSUS_C439ADE_DEPLOYED_AT", raising=False)

    with pytest.raises(SystemExit) as exc:
        brief.mode_census_prepare()

    assert exc.value.code == 2
    assert "CENSUS_C439ADE_DEPLOYED_AT" in capsys.readouterr().out


def test_unparseable_env_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("CENSUS_C439ADE_DEPLOYED_AT", "not-a-date")

    with pytest.raises(SystemExit) as exc:
        brief.mode_census_prepare()

    assert exc.value.code == 2
    assert "not-a-date" in capsys.readouterr().out


def test_naive_env_is_rejected_r9(monkeypatch, capsys):
    """Ruling R9: a value with no timezone offset is rejected, not silently
    accepted and stored under whatever TimeZone the connection has."""
    monkeypatch.setenv("CENSUS_C439ADE_DEPLOYED_AT", "2026-09-25T12:00:00")

    with pytest.raises(SystemExit) as exc:
        brief.mode_census_prepare()

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "timezone" in out
    assert "2026-09-25T12:00:00" in out


def test_lock_not_acquired_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("CENSUS_C439ADE_DEPLOYED_AT", "2026-09-25T12:00:00+00:00")

    @contextlib.contextmanager
    def _lock_denied(conn, name):
        yield False

    monkeypatch.setattr(db, "advisory_lock", _lock_denied)

    with pytest.raises(SystemExit) as exc:
        brief.mode_census_prepare()

    assert exc.value.code == 2
    assert "already running" in capsys.readouterr().out


def test_census_refusal_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("CENSUS_C439ADE_DEPLOYED_AT", "2026-09-25T12:00:00+00:00")

    def _refuse(conn, today, c439ade_deployed_at, now, gap_ruling=None):
        raise census.CensusRefusal("synthetic refusal for the CLI test")

    monkeypatch.setattr(census, "prepare", _refuse)

    with pytest.raises(SystemExit) as exc:
        brief.mode_census_prepare()

    assert exc.value.code == 2
    assert "synthetic refusal for the CLI test" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [(None, None), ("", None), ("within_6h_only", "within_6h_only")],
)
def test_gap_ruling_env_reaches_prepare(monkeypatch, capsys, env_value, expected):
    """CENSUS_GAP_RULING is passed through verbatim (empty means none);
    `census.prepare` alone decides whether the value is a valid ruling."""
    monkeypatch.setenv("CENSUS_C439ADE_DEPLOYED_AT", "2026-09-25T12:00:00+00:00")
    if env_value is None:
        monkeypatch.delenv("CENSUS_GAP_RULING", raising=False)
    else:
        monkeypatch.setenv("CENSUS_GAP_RULING", env_value)
    seen = {}

    def _capture(conn, today, c439ade_deployed_at, now, gap_ruling=None):
        seen["gap_ruling"] = gap_ruling
        return "captured"

    monkeypatch.setattr(census, "prepare", _capture)

    brief.mode_census_prepare()

    assert seen == {"gap_ruling": expected}
    assert "captured" in capsys.readouterr().out
