"""Backup job: version guard, dump construction, retention, atomicity.

The version guard is the reason most of this file exists. pg_dump is FORWARD
compatible only — a newer client dumps an older server fine, but a client older
than the server aborts and writes nothing. Debian trixie (the base image) ships
client 17 while the stack runs postgres 18, so the wrong install line produces a
job that fails every night and looks, from the outside, exactly like a job that
has not run yet.
"""

import os
import subprocess
from types import SimpleNamespace

import pytest

import backup as bk


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


class _Runner:
    """Stand-in for subprocess.run that records calls and replays a result."""

    def __init__(self, returncode=0, stderr="", write_target=True):
        self.returncode = returncode
        self.stderr = stderr
        self.write_target = write_target
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.write_target and self.returncode == 0:
            # Mimic pg_dump writing the file named by --file.
            target = cmd[cmd.index("--file") + 1]
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("PGDMP fake")
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


# ── version parsing and the guard ─────────────────────────────────────────────


def test_client_major_parses_plain():
    assert bk._client_major("pg_dump (PostgreSQL) 18.1") == 18


def test_client_major_parses_debian_suffix():
    assert bk._client_major("pg_dump (PostgreSQL) 17.6 (Debian 17.6-1.pgdg13+1)") == 17


def test_client_major_unparseable_returns_none():
    assert bk._client_major("") is None
    assert bk._client_major("bash: pg_dump: command not found") is None


def test_server_major_from_version_num():
    assert bk._server_major(180003) == 18
    assert bk._server_major(170006) == 17


def test_version_refusal_when_client_older_than_server():
    reason = bk.version_refusal(17, 18)
    assert reason is not None
    assert "17" in reason and "18" in reason


def test_version_refusal_allows_equal_and_newer_client():
    assert bk.version_refusal(18, 18) is None
    assert bk.version_refusal(19, 18) is None


def test_version_refusal_when_client_missing():
    assert bk.version_refusal(None, 18) is not None


# ── the dump command must never carry the password ────────────────────────────


def _parts():
    return {
        "host": "postgres",
        "port": "5432",
        "user": "newsbrief",
        "password": "s3cr3t-in-argv-is-a-bug",
        "dbname": "newsbrief",
    }


def test_dump_command_carries_connection_but_not_password(tmp_path):
    target = tmp_path / "newsbrief-2026-09-01.dump"
    cmd = bk.dump_command(_parts(), target)
    joined = " ".join(cmd)
    assert "s3cr3t-in-argv-is-a-bug" not in joined
    assert "postgres" in cmd and "5432" in cmd
    assert "newsbrief" in cmd
    assert str(target) in cmd
    assert "--format=custom" in cmd


def test_dump_env_carries_password_and_keeps_environ(monkeypatch):
    monkeypatch.setenv("SOME_UNRELATED", "kept")
    env = bk.dump_env(_parts())
    assert env["PGPASSWORD"] == "s3cr3t-in-argv-is-a-bug"
    assert env["SOME_UNRELATED"] == "kept"
    # The caller's own environment must not be mutated.
    assert "PGPASSWORD" not in os.environ


def test_dump_env_omits_pgpassword_when_no_password():
    parts = _parts()
    del parts["password"]
    assert "PGPASSWORD" not in bk.dump_env(parts)


# ── retention window ──────────────────────────────────────────────────────────


def test_resolve_days_default(monkeypatch):
    monkeypatch.delenv(bk.RETENTION_DAYS_ENV, raising=False)
    assert bk._resolve_days(None) == bk.DEFAULT_RETENTION_DAYS == 14


def test_resolve_days_env_override(monkeypatch):
    monkeypatch.setenv(bk.RETENTION_DAYS_ENV, "30")
    assert bk._resolve_days(None) == 30


def test_resolve_days_invalid_falls_back(monkeypatch):
    monkeypatch.setenv(bk.RETENTION_DAYS_ENV, "not-a-number")
    assert bk._resolve_days(None) == 14


def test_resolve_days_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv(bk.RETENTION_DAYS_ENV, "30")
    assert bk._resolve_days(3) == 3


def test_prune_deletes_old_keeps_recent_boundary_and_foreign(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    d = tmp_path / bk.BACKUP_DIR_NAME
    _touch(d / "newsbrief-2026-08-01.dump")  # old -> delete
    _touch(d / "newsbrief-2026-08-18.dump")  # exactly cutoff -> keep
    _touch(d / "newsbrief-2026-09-01.dump")  # today -> keep
    _touch(d / "newsbrief-2026-13-99.dump")  # invalid date -> keep
    _touch(d / "notes.txt")  # not a dump -> keep

    deleted = bk.prune_dumps("2026-09-01", 14)  # cutoff = 2026-08-18

    assert deleted == 1
    assert not (d / "newsbrief-2026-08-01.dump").exists()
    assert (d / "newsbrief-2026-08-18.dump").exists()
    assert (d / "newsbrief-2026-09-01.dump").exists()
    assert (d / "newsbrief-2026-13-99.dump").exists()
    assert (d / "notes.txt").exists()


def test_prune_disabled_by_non_positive_days(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    d = tmp_path / bk.BACKUP_DIR_NAME
    _touch(d / "newsbrief-2020-01-01.dump")
    assert bk.prune_dumps("2026-09-01", 0) == 0
    assert (d / "newsbrief-2020-01-01.dump").exists()


def test_prune_is_fail_safe_when_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    assert bk.prune_dumps("2026-09-01", 14) == 0


def test_prune_survives_unlink_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    d = tmp_path / bk.BACKUP_DIR_NAME
    _touch(d / "newsbrief-2026-08-01.dump")

    def _boom(self):
        raise OSError("held open by something")

    monkeypatch.setattr("pathlib.Path.unlink", _boom)
    assert bk.prune_dumps("2026-09-01", 14) == 0  # counted none, raised nothing


# ── create_dump: atomicity and failure ────────────────────────────────────────


def test_create_dump_writes_target_and_leaves_no_part_file(tmp_path):
    target = tmp_path / "newsbrief-2026-09-01.dump"
    runner = _Runner()
    bk.create_dump(_parts(), target, runner=runner)
    assert target.exists()
    assert not list(tmp_path.glob("*.part"))
    # pg_dump wrote to the part file, not the final name.
    cmd = runner.calls[0][0]
    assert cmd[cmd.index("--file") + 1].endswith(".part")


def test_create_dump_raises_and_cleans_up_on_failure(tmp_path):
    target = tmp_path / "newsbrief-2026-09-01.dump"
    runner = _Runner(returncode=1, stderr="connection refused", write_target=True)
    with pytest.raises(RuntimeError, match="connection refused"):
        bk.create_dump(_parts(), target, runner=runner)
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_create_dump_raises_when_dump_produced_nothing(tmp_path):
    """Exit 0 with no file is a lie the ledger must not record as success."""
    target = tmp_path / "newsbrief-2026-09-01.dump"
    runner = _Runner(returncode=0, write_target=False)
    with pytest.raises(RuntimeError):
        bk.create_dump(_parts(), target, runner=runner)
    assert not target.exists()


# ── run_backup wiring ─────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, version_num=180003):
        self.version_num = version_num

    def execute(self, sql, params=None):
        return SimpleNamespace(fetchone=lambda: (self.version_num,))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_backup_refuses_on_version_mismatch_without_dumping(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    runner = _Runner()
    monkeypatch.setattr(
        bk, "_probe_client_version", lambda: "pg_dump (PostgreSQL) 17.6"
    )
    monkeypatch.setattr(bk.db, "connect", lambda **kw: _FakeConn(180003))
    monkeypatch.setattr(bk.db, "conninfo", lambda: "dbname=newsbrief")

    with pytest.raises(RuntimeError, match="pg_dump"):
        bk.run_backup(today="2026-09-01", runner=runner)

    assert runner.calls == []


def test_run_backup_writes_dump_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "DATA_DIR", tmp_path)
    old = tmp_path / bk.BACKUP_DIR_NAME / "newsbrief-2026-08-01.dump"
    _touch(old)
    runner = _Runner()
    monkeypatch.setattr(
        bk, "_probe_client_version", lambda: "pg_dump (PostgreSQL) 18.1"
    )
    monkeypatch.setattr(bk.db, "connect", lambda **kw: _FakeConn(180003))
    monkeypatch.setattr(
        bk.db,
        "conninfo",
        lambda: "host=postgres user=newsbrief password=pw dbname=newsbrief",
    )

    summary = bk.run_backup(today="2026-09-01", runner=runner)

    assert (tmp_path / bk.BACKUP_DIR_NAME / "newsbrief-2026-09-01.dump").exists()
    assert not old.exists()
    assert summary["pruned"] == 1
    assert summary["bytes"] > 0


def test_run_backup_default_runner_is_subprocess_run():
    """Guards against a test-only default silently shipping."""
    import inspect

    assert inspect.signature(bk.run_backup).parameters["runner"].default is None
    assert bk._DEFAULT_RUNNER is subprocess.run
