"""file_lock: cross-process mutual exclusion for read-merge-write state.

Atomic writes (_write_json_atomic) stop torn files but NOT lost updates: two
processes that each load -> mutate -> save can clobber each other. file_lock
serialises those spans. The paper book feeds the go-live gate, so a lost write
there is money-adjacent — hence the dedicated coverage.
"""

import os
import time

import pytest

import common


def test_file_lock_is_exclusive_while_held(tmp_path):
    lock = tmp_path / "x.lock"
    with common.file_lock(lock, timeout=0.3):
        with pytest.raises(common.LockTimeout):
            with common.file_lock(lock, timeout=0.3):
                pass


def test_file_lock_releases_on_normal_exit(tmp_path):
    lock = tmp_path / "x.lock"
    with common.file_lock(lock, timeout=0.3):
        pass
    # Second acquisition must succeed — no leaked lock file.
    with common.file_lock(lock, timeout=0.3):
        pass
    assert not lock.exists()


def test_file_lock_releases_even_on_exception(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(ValueError):
        with common.file_lock(lock, timeout=0.3):
            raise ValueError("boom")
    # The lock must not survive the exception.
    with common.file_lock(lock, timeout=0.3):
        pass


def test_file_lock_breaks_stale_lock(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("424242")  # orphan from a crashed process
    old = time.time() - 3600
    os.utime(lock, (old, old))
    # Older than stale_after -> broken and re-acquired rather than timing out.
    with common.file_lock(lock, timeout=0.3, stale_after=60):
        pass


def test_file_lock_derives_lockfile_from_target_path(tmp_path):
    # Passing the data file itself locks on "<file>.lock", never the data file.
    target = tmp_path / "book.json"
    target.write_text('{"positions": []}')
    with common.file_lock(target, timeout=0.3):
        assert (tmp_path / "book.json.lock").exists()
        assert target.read_text() == '{"positions": []}'  # data untouched
