"""The group job (plan 1, task 6): wiring with stubs, plus a DB-gated cost line.

Text is invented; the repo is public.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brief
import common
import db
import embeddings
import group_store as gs
import grouping
import scheduler

SPACE = "modal:Test/Model@1024"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class FakeConn:
    def commit(self):
        pass

    def rollback(self):
        pass


class FakeEmbedder:
    space = SPACE


def _out(status, error=None, **extra):
    out = {
        "status": status,
        "error": error,
        "embedded": 0,
        "reused": 0,
        "skipped": 0,
        "calls": 0,
        "grouped": 0,
        "opened": 0,
        "joined": 0,
        "stopped_at_missing_vector": False,
    }
    out.update(extra)
    return out


class Env:
    """Stubs everything mode_group touches and records what it did."""

    def __init__(self, monkeypatch, *, status="ok", error=None, backlog_h=None):
        self.state, self.sent, self.pass_calls = {}, [], []
        self.status, self.error = status, error
        self.backlog_h = backlog_h
        self.embedder, self.problem = FakeEmbedder(), None
        mp = monkeypatch
        mp.setattr(brief, "load_state", lambda: dict(self.state))
        mp.setattr(brief, "save_state", lambda d: self.state.update(d))
        # All three send channels, on brief and on common: a test that patches
        # one passes vacuously when the code reaches another.
        for name in ("telegram_alert", "telegram_send", "telegram_send_long"):
            mp.setattr(brief, name, self._sink(name))
            mp.setattr(common, name, self._sink(name))
        mp.setattr(
            brief.config,
            "clear_runtime_state",
            lambda keys: [self.state.pop(k, None) for k in keys],
        )

        class Ctx:
            def __enter__(s):
                return FakeConn()

            def __exit__(s, *a):
                return False

        mp.setattr(brief.db, "connect", lambda: Ctx())
        mp.setattr(common, "STORYLINE_GROUPING_ENABLED", True)
        mp.setattr(common, "STORYLINE_GROUP_WINDOW_HOURS", 72)
        mp.setattr(common, "EMBEDDING_DIM", 1024)
        mp.setattr(embeddings, "active_embedder", lambda: (self.embedder, self.problem))
        mp.setattr(grouping, "group_pass", self._group_pass)
        mp.setattr(gs, "oldest_ungrouped_created_at", self._oldest)

    def _sink(self, name):
        return lambda m, *a, **k: self.sent.append((name, m))

    def _group_pass(self, conn, embedder, **kw):
        self.pass_calls.append((embedder, kw))
        return _out(self.status, self.error)

    def _oldest(self, conn, *, space):
        if self.backlog_h is None:
            return None
        return datetime.now(timezone.utc) - timedelta(hours=self.backlog_h)

    def alerts(self):
        return [m for n, m in self.sent if n == "telegram_alert"]


def test_mode_group_off_does_nothing(monkeypatch, caplog):
    env = Env(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_GROUPING_ENABLED", False)
    with caplog.at_level(logging.INFO):
        brief.mode_group()
    assert "Group: off" in caplog.text
    assert env.pass_calls == [] and env.sent == []


def test_mode_group_passes_the_live_cap_window_dim_and_space(monkeypatch):
    env = Env(monkeypatch, status="nothing_to_do")
    brief.mode_group()
    ((embedder, kw),) = env.pass_calls
    assert embedder is env.embedder
    assert kw["space"] == SPACE
    assert kw["dim"] == 1024
    assert kw["window_hours"] == 72
    assert kw["max_items"] == grouping.LIVE_MAX_ITEMS


def test_mode_group_rings_on_config_and_names_the_problem(monkeypatch):
    env = Env(monkeypatch, backlog_h=0.5)
    env.embedder, env.problem = None, "EMBEDDING_MODAL_URL is empty"
    brief.mode_group()
    brief.mode_group()  # the same fault rings once
    assert env.pass_calls == []  # no embedder, so no call and no embedding_calls row
    (msg,) = env.alerts()
    assert "config" in msg and "EMBEDDING_MODAL_URL is empty" in msg
    assert "0.5" in msg  # the backlog age is named


def test_mode_group_rings_on_uncalibrated(monkeypatch):
    env = Env(
        monkeypatch,
        status="uncalibrated",
        error=f"no calibration for space {SPACE}: vectors stored, nothing grouped",
    )
    brief.mode_group()
    brief.mode_group()
    (msg,) = env.alerts()
    assert "uncalibrated" in msg and SPACE in msg


@pytest.mark.parametrize("status", ["config", "bad_response", "input"])
def test_hard_faults_ring_straight_away(monkeypatch, status):
    env = Env(monkeypatch, status=status, error="boom", backlog_h=0.1)
    brief.mode_group()
    (msg,) = env.alerts()
    assert status in msg and "boom" in msg


def test_transient_rings_only_when_the_backlog_is_old(monkeypatch):
    env = Env(monkeypatch, status="transient", error="timeout", backlog_h=1)
    brief.mode_group()
    assert env.alerts() == []
    env.backlog_h = 3
    brief.mode_group()
    (msg,) = env.alerts()
    assert "transient" in msg and "timeout" in msg and "3.0" in msg


def test_transient_with_no_backlog_does_not_ring(monkeypatch):
    env = Env(monkeypatch, status="transient", error="timeout", backlog_h=None)
    brief.mode_group()
    assert env.alerts() == []


def test_ok_with_no_backlog_clears_the_alert(monkeypatch):
    env = Env(monkeypatch, status="ok", backlog_h=None)
    env.state["group_alert"] = "config:x"
    brief.mode_group()
    assert "group_alert" not in env.state and env.alerts() == []


def test_nothing_to_do_under_the_threshold_clears_the_alert(monkeypatch):
    env = Env(monkeypatch, status="nothing_to_do", backlog_h=1)
    env.state["group_alert"] = "config:x"
    brief.mode_group()
    assert "group_alert" not in env.state


def test_ok_with_an_old_backlog_does_not_clear_the_alert(monkeypatch):
    env = Env(monkeypatch, status="ok", backlog_h=5)
    env.state["group_alert"] = "transient"
    brief.mode_group()
    assert env.state.get("group_alert") == "transient" and env.alerts() == []


@pytest.mark.parametrize("status", ["busy", "transient"])
def test_busy_and_a_young_transient_neither_ring_nor_clear(monkeypatch, status):
    env = Env(monkeypatch, status=status, error="x", backlog_h=0.2)
    env.state["group_alert"] = "config:x"
    brief.mode_group()
    assert env.state.get("group_alert") == "config:x" and env.alerts() == []


def test_log_line_carries_every_count_and_the_backlog(monkeypatch, caplog):
    env = Env(monkeypatch, status="ok", backlog_h=1.26)
    with caplog.at_level(logging.INFO):
        brief.mode_group()
    line = next(r.getMessage() for r in caplog.records if "Group:" in r.getMessage())
    for part in (
        "status=ok",
        "embedded=0",
        "reused=0",
        "grouped=0",
        "opened=0",
        "joined=0",
        "calls=0",
        "backlog_h=1.3",
    ):
        assert part in line, line
    env.backlog_h = None
    caplog.clear()
    with caplog.at_level(logging.INFO):
        brief.mode_group()
    assert "backlog_h=-" in caplog.text


def test_group_is_scheduled_every_30_minutes_and_is_a_job_mode():
    spec = next(s for s in scheduler.SCHEDULES if s.job == "group")
    assert (spec.kind, spec.every_minutes, spec.grace_minutes) == ("interval", 30, 10)
    assert brief.MODES["group"] is brief.mode_group
    assert "group" in brief.JOB_MODES
    assert brief.GROUP_BACKLOG_ALERT_HOURS == 2


def test_knobs_default_off_and_compose_passes_them():
    assert common.KNOBS["STORYLINE_GROUPING_ENABLED"].default is False
    assert common.KNOBS["STORYLINE_GROUP_WINDOW_HOURS"].default == 72
    text = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert "- STORYLINE_GROUPING_ENABLED=${STORYLINE_GROUPING_ENABLED:-}" in text
    assert "- STORYLINE_GROUP_WINDOW_HOURS=${STORYLINE_GROUP_WINDOW_HOURS:-}" in text


# -- the weekly cost line -----------------------------------------------------


def _month(modal=None, openai=None):
    d = {}
    if modal:
        d["modal"] = modal
    if openai:
        d["openai"] = openai
    return d


MODAL = {
    "usd": 0.5,
    "openai_equivalent_usd": 1.25,
    "calls": 12,
    "cold_starts": 3,
    "tokens": 1000,
}


def test_cost_line_is_none_without_calls(monkeypatch):
    monkeypatch.setattr(gs, "embedding_month_to_date", lambda c, *, now: {})
    assert brief.embedding_cost_line(FakeConn(), now=NOW) is None


def test_cost_line_text_for_modal_only(monkeypatch):
    monkeypatch.setattr(gs, "embedding_month_to_date", lambda c, *, now: _month(MODAL))
    line = brief.embedding_cost_line(FakeConn(), now=NOW)
    assert line == (
        "🧮 Embeddings this month: Modal $0.50 (12 calls, 3 cold starts);"
        " the same tokens on OpenAI ≈ $1.25."
    )


def test_cost_line_adds_openai_spend_when_there_is_some(monkeypatch):
    oa = dict(MODAL, usd=0.07, openai_equivalent_usd=0.07, calls=2, cold_starts=0)
    monkeypatch.setattr(
        gs, "embedding_month_to_date", lambda c, *, now: _month(MODAL, oa)
    )
    line = brief.embedding_cost_line(FakeConn(), now=NOW)
    assert line.endswith("≈ $1.25. OpenAI: $0.07.")


class _WeeklyEnv:
    def __init__(self, monkeypatch, line):
        self.events = []
        mp = monkeypatch
        mp.setattr(brief, "load_last_n_briefs", lambda n: [("2026-11-23", "b")])
        mp.setattr(brief, "build_weekly_prompt", lambda b: "p")
        mp.setattr(brief, "submit_batch_no_search", lambda *a, **k: "bid")
        mp.setattr(brief, "poll_batch", lambda *a, **k: "summary")
        mp.setattr(brief, "deliver", lambda *a, **k: self.events.append("deliver"))
        mp.setattr(brief, "refresh_instruments_cache", lambda **k: None)
        mp.setattr(brief, "load_book", lambda: {})
        mp.setattr(brief, "mark_to_market", lambda b, d: b)
        mp.setattr(brief, "save_book", lambda b: None)
        mp.setattr(brief, "record_gate_history", lambda b: None)
        mp.setattr(brief, "performance_report", lambda b: "perf")
        mp.setattr(
            brief, "telegram_send_long", lambda m, **k: self.events.append("perf")
        )
        mp.setattr(db, "is_configured", lambda: True)

        class Ctx:
            def __enter__(s):
                return FakeConn()

            def __exit__(s, *a):
                return False

        mp.setattr(brief.db, "connect", lambda: Ctx())
        mp.setattr(brief, "embedding_cost_line", lambda conn, *, now: line)
        mp.setattr(
            common, "telegram_send", lambda m, **k: self.events.append(("cost", m))
        )


@pytest.fixture
def no_file_lock(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def fake(path):
        yield

    monkeypatch.setattr(brief, "file_lock", fake)


def test_weekly_sends_the_cost_line_after_the_summary(monkeypatch, no_file_lock):
    env = _WeeklyEnv(monkeypatch, "COST LINE")
    brief.mode_weekly()
    assert ("cost", "COST LINE") in env.events
    assert env.events.index("deliver") < env.events.index(("cost", "COST LINE"))


def test_weekly_sends_nothing_when_the_month_has_no_calls(monkeypatch, no_file_lock):
    env = _WeeklyEnv(monkeypatch, None)
    brief.mode_weekly()
    assert not [e for e in env.events if isinstance(e, tuple)]
    assert "deliver" in env.events and "perf" in env.events


def test_weekly_survives_a_cost_line_failure(monkeypatch, no_file_lock):
    env = _WeeklyEnv(monkeypatch, None)

    def boom(conn, *, now):
        raise RuntimeError("db down")

    monkeypatch.setattr(brief, "embedding_cost_line", boom)
    brief.mode_weekly()
    assert "perf" in env.events


# -- DB-gated -----------------------------------------------------------------


@pytest.mark.skipif(not db.is_configured(), reason="No database is configured")
def test_embedding_cost_line_shows_both_providers_and_the_equivalent():
    month = datetime(2026, 11, 10, tzinfo=timezone.utc)
    last_month = datetime(2026, 10, 28, tzinfo=timezone.utc)
    conn = db.connect()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        db.run_migrations(conn)
        conn.commit()

        def row(provider, usd, equiv, cold, when, tokens=100):
            conn.execute(
                "INSERT INTO embedding_calls (provider, space, purpose, inputs,"
                " tokens, cold_start, usd, openai_equivalent_usd, outcome,"
                " created_at) VALUES (%s, %s, 'items', 1, %s, %s, %s, %s, 'ok', %s)",
                (provider, f"{provider}:m@8", tokens, cold, usd, equiv, when),
            )

        row("modal", "0.30", "0.40", True, month)
        row("modal", "0.20", "0.35", False, month)
        row("openai", "0.07", "0.07", None, month)
        row("modal", "9.00", "9.00", True, last_month)  # not this month
        conn.commit()
        out = gs.embedding_month_to_date(conn, now=NOW)
        line = brief.embedding_cost_line(conn, now=NOW)
    finally:
        conn.close()
    assert out["modal"]["calls"] == 2 and out["modal"]["cold_starts"] == 1
    assert out["modal"]["tokens"] == 200
    assert out["modal"]["usd"] == pytest.approx(0.5)
    assert out["modal"]["openai_equivalent_usd"] == pytest.approx(0.75)
    assert out["openai"]["usd"] == pytest.approx(0.07)
    assert line == (
        "🧮 Embeddings this month: Modal $0.50 (2 calls, 1 cold starts);"
        " the same tokens on OpenAI ≈ $0.75. OpenAI: $0.07."
    )
