
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
