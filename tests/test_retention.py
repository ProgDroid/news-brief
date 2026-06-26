import json

import retention as rt


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


def test_file_date_extracts():
    assert rt._file_date("brief-2026-06-26.md").isoformat() == "2026-06-26"
    assert rt._file_date("source_index-2026-01-02.json").isoformat() == "2026-01-02"


def test_file_date_undateable_returns_none():
    assert rt._file_date("signals-log.jsonl") is None
    assert rt._file_date("book.json") is None
    assert rt._file_date("brief-2026-13-99.md") is None  # invalid calendar date


def test_resolve_days_default(monkeypatch):
    monkeypatch.delenv("NEWSBRIEF_RETENTION_DAYS", raising=False)
    assert rt._resolve_days(None) == 90


def test_resolve_days_env_override(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "30")
    assert rt._resolve_days(None) == 30


def test_resolve_days_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "abc")
    assert rt._resolve_days(None) == 90


def test_resolve_days_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("NEWSBRIEF_RETENTION_DAYS", "30")
    assert rt._resolve_days(5) == 5


def test_prune_deletes_old_keeps_recent_undateable_and_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    briefs = tmp_path / "briefs"
    _touch(briefs / "brief-2026-01-01.md")  # old -> delete
    _touch(briefs / "brief-2026-06-25.md")  # recent -> keep
    _touch(tmp_path / "source_index-2026-01-01.json")  # old -> delete
    _touch(tmp_path / "claim_evidence-2026-03-28.json")  # exactly cutoff -> keep
    _touch(tmp_path / "verification-2026-06-20.json")  # recent -> keep
    _touch(tmp_path / "enrichment" / "enrichment-2026-01-01.json")  # old -> delete
    _touch(tmp_path / "signals" / "signals-2026-06-25.json")  # recent -> keep
    _touch(tmp_path / "book.json")  # undateable -> keep
    _touch(tmp_path / "signals" / "signals-log.jsonl")  # undateable -> keep

    deleted = rt.prune_dated_files("2026-06-26", 90)  # cutoff = 2026-03-28

    assert deleted == 3
    assert not (briefs / "brief-2026-01-01.md").exists()
    assert (briefs / "brief-2026-06-25.md").exists()
    assert not (tmp_path / "source_index-2026-01-01.json").exists()
    assert (tmp_path / "claim_evidence-2026-03-28.json").exists()  # boundary kept
    assert (tmp_path / "verification-2026-06-20.json").exists()
    assert not (tmp_path / "enrichment" / "enrichment-2026-01-01.json").exists()
    assert (tmp_path / "signals" / "signals-2026-06-25.json").exists()
    assert (tmp_path / "book.json").exists()
    assert (
        tmp_path / "signals" / "signals-log.jsonl"
    ).exists()  # not matched by signals-*.json


def test_prune_missing_dirs_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)  # no subdirs exist
    assert rt.prune_dated_files("2026-06-26", 90) == 0


def test_trim_drops_old_keeps_recent_malformed_and_nodate(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    log_path = sig / "signals-log.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"ticker": "OLD", "date": "2026-01-01"}),  # old -> drop
                json.dumps({"ticker": "NEW", "date": "2026-06-25"}),  # recent -> keep
                json.dumps({"ticker": "NODATE"}),  # no date -> keep
                "{not valid json",  # malformed -> keep
            ]
        )
        + "\n"
    )
    dropped = rt.trim_signals_log("2026-06-26", 90)
    assert dropped == 1
    remaining = log_path.read_text()
    assert "OLD" not in remaining
    assert "NEW" in remaining
    assert "NODATE" in remaining
    assert "not valid json" in remaining
    # file is still valid: every non-empty line that is JSON parses
    for line in remaining.splitlines():
        if line.strip() and not line.startswith("{not"):
            json.loads(line)


def test_trim_absent_file_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    assert rt.trim_signals_log("2026-06-26", 90) == 0


def test_trim_nothing_old_leaves_file_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    p = sig / "signals-log.jsonl"
    p.write_text(json.dumps({"date": "2026-06-25"}) + "\n")
    assert rt.trim_signals_log("2026-06-26", 90) == 0
    assert "2026-06-25" in p.read_text()


def test_run_retention_disabled_when_days_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    _touch(tmp_path / "source_index-2020-01-01.json")
    out = rt.run_retention("2026-06-26", days=0)
    assert out == {"deleted": 0, "trimmed_lines": 0}
    assert (tmp_path / "source_index-2020-01-01.json").exists()  # nothing deleted


def test_run_retention_happy_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)
    _touch(tmp_path / "source_index-2020-01-01.json")  # very old -> delete
    sig = tmp_path / "signals"
    sig.mkdir(parents=True)
    (sig / "signals-log.jsonl").write_text(json.dumps({"date": "2020-01-01"}) + "\n")
    out = rt.run_retention("2026-06-26", days=90)
    assert out["deleted"] == 1
    assert out["trimmed_lines"] == 1


def test_run_retention_fail_safe_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "DATA_DIR", tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rt, "prune_dated_files", boom)
    out = rt.run_retention("2026-06-26", days=90)  # must not raise
    assert out == {"deleted": 0, "trimmed_lines": 0}
