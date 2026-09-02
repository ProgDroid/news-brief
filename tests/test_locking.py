"""file_lock: cross-process mutual exclusion for read-merge-write state.

Atomic writes (_write_json_atomic) stop torn files but NOT lost updates: two
processes that each load -> mutate -> save can clobber each other. file_lock
serialises those spans. The paper book feeds the go-live gate, so a lost write
there is money-adjacent — hence the dedicated coverage.
"""

import json
import os
import time
from datetime import datetime, timezone

import pytest

import brief
import config
import common
import trading


def _fb():
    return {"focus": [], "mute": [], "notes": []}


def _update(text):
    return {"message": {"text": text, "chat": {"id": config.chat_id()}}}


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


# ── Wiring: the lock is actually engaged around the read-merge-write spans ──────
def test_save_state_updates_only_the_keys_it_was_given(monkeypatch):
    """What the file lock was protecting, asserted directly.

    `save_state` used to rewrite the whole document, so a lock was needed to
    stop the daemon's `tg_offset` write from dropping a `batch_id` that submit
    had written since the daemon's read. It is a per-key upsert now: this
    asserts the property the lock existed to provide, which survives the
    substrate change, rather than the lock itself, which does not.
    """
    written: list[dict] = []
    monkeypatch.setattr(config, "set_runtime_state", written.append)

    brief.save_state({"tg_offset": 7})

    assert written == [{"tg_offset": 7}], (
        "save_state must pass through exactly the keys it was given — a writer "
        "that also sends back keys it read is how the other process's value is lost"
    )


def test_close_command_writes_book_under_lock(monkeypatch, tmp_path):
    book_file = tmp_path / "book.json"
    monkeypatch.setattr(trading, "BOOK_FILE", book_file)
    monkeypatch.setattr(brief, "telegram_send", lambda m: True)
    trading.save_book(
        {
            "positions": [
                {
                    "status": "open",
                    "ticker": "BP",
                    "direction": "bullish",
                    "asset_class": "equity",
                    "instrument": "bp.uk",
                    "entry_price": 5.0,
                }
            ]
        }
    )
    monkeypatch.setattr(
        brief,
        "_close_position_at_market",
        lambda p, day, reason: p.__setitem__("status", "closed") or True,
    )
    seen = {}
    real_save = brief.save_book

    def spy_save(book):
        seen["lock_held"] = (tmp_path / "book.json.lock").exists()
        return real_save(book)

    monkeypatch.setattr(brief, "save_book", spy_save)
    brief._handle_telegram_update(_update("/close BP"), _fb())

    assert seen["lock_held"] is True
    assert not (tmp_path / "book.json.lock").exists()  # released on exit


def test_mode_paper_writes_book_under_lock(monkeypatch, tmp_path):
    # The canonical race the lock exists for: collect's mode_paper write vs a
    # concurrent /close. mode_paper must hold the book lock across its save.
    book_file = tmp_path / "book.json"
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    monkeypatch.setattr(trading, "BOOK_FILE", book_file)
    monkeypatch.setattr(trading, "SIGNALS_DIR", sig_dir)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # A non-actionable signal: mode_paper still runs the load->save span (opens 0),
    # so no network is touched (PolyGram is unconfigured in the test env).
    (sig_dir / f"signals-{today}.json").write_text(
        json.dumps({"signals": [{"direction": "neutral", "ticker": "X"}]})
    )
    seen = {}
    real_save = trading.save_book

    def spy_save(book):
        seen["lock_held"] = (tmp_path / "book.json.lock").exists()
        return real_save(book)

    monkeypatch.setattr(trading, "save_book", spy_save)
    trading.mode_paper()

    assert seen["lock_held"] is True
    assert not (tmp_path / "book.json.lock").exists()  # released on exit
