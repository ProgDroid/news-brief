"""Brief section parser and the 7-day headline window (no database)."""

import dated_memory


def TOP(*entries):
    return "🌍 TOP STORIES\n" + "".join(f"• {e}\n" for e in entries)


def test_sections_split_on_known_and_unknown_headings():
    text = (
        "🌍 TOP STORIES\n• Alpha happened\n• Beta happened\n\n"
        "📈 MARKET PULSE — WHAT MOVED\n• Gold up\n\n"
        "👁 WATCH / FORWARD\n• Vote on Thursday\n"
    )
    s = dated_memory.brief_sections(text)
    assert s["top"] == ["Alpha happened", "Beta happened"]
    assert s["watch"] == ["Vote on Thursday"]
    assert "Gold up" not in str(s)


def test_all_four_known_headings_map_to_their_keys():
    text = (
        "🌍 TOP STORIES\n• a\n\n🧵 THREADS THAT MOVED\n• b\n\n"
        "👁 WATCH / FORWARD\n• c\n\n📌 POSITION SIGNALS\n• d\n"
    )
    assert dated_memory.brief_sections(text) == {
        "top": ["a"],
        "threads": ["b"],
        "watch": ["c"],
        "positions": ["d"],
    }


def test_dynamic_uppercase_heading_closes_top_stories():
    text = "🌍 TOP STORIES\n• Alpha\n🇯🇵 JAPAN\n• Yen weak\n"
    s = dated_memory.brief_sections(text)
    assert s == {"top": ["Alpha"]}


def test_continuation_line_joins_its_entry():
    text = "🌍 TOP STORIES\n• First line\nsecond line\n, third\n— fourth\n• Next\n"
    s = dated_memory.brief_sections(text)
    assert s["top"] == ["First line\nsecond line\n, third\n— fourth", "Next"]


def test_dash_bullet_and_non_bullet_after_heading_or_blank_start_entries():
    text = "🌍 TOP STORIES\nPlain one\ncont\n\nPlain two\n- Dashed\n"
    assert dated_memory.brief_sections(text)["top"] == [
        "Plain one\ncont",
        "Plain two",
        "Dashed",
    ]


def test_html_tags_and_entities_are_removed():
    s = dated_memory.brief_sections("<b>🌍 TOP STORIES</b>\n• A &amp; B\n")
    assert s["top"] == ["A & B"]


def test_missing_positions_section_is_absent_not_empty():
    s = dated_memory.brief_sections(TOP("x"))
    assert "positions" not in s
    assert dated_memory.brief_sections("nothing here") == {}
    assert dated_memory.brief_sections("") == {}


def test_known_heading_with_no_entries_is_absent():
    assert dated_memory.brief_sections("🌍 TOP STORIES\n\n📌 POSITION SIGNALS\n") == {}


def test_lines_before_any_heading_are_ignored():
    assert dated_memory.brief_sections("• stray\nGood morning\n" + TOP("x")) == {
        "top": ["x"]
    }


def test_window_keeps_only_the_seven_days_before_today():
    briefs = [
        ("2026-11-10", TOP("old")),
        ("2026-11-15", TOP("kept")),
        ("2026-11-22", TOP("same day")),
    ]
    w = dated_memory.build_window(briefs, "2026-11-22")
    assert "11-15 [top] kept" in w.block
    assert "old" not in w.block and "same day" not in w.block
    assert w.days == 1 and w.lines == 1


def test_window_boundaries_are_inclusive_at_minus_seven_and_minus_one():
    briefs = [
        ("2026-11-14", TOP("edge-old")),
        ("2026-11-15", TOP("edge-in")),
        ("2026-11-21", TOP("edge-new")),
    ]
    w = dated_memory.build_window(briefs, "2026-11-22")
    assert "edge-in" in w.block and "edge-new" in w.block
    assert "edge-old" not in w.block
    assert w.days == 2


def test_window_is_empty_with_no_qualifying_brief():
    for briefs in ([], [("2026-11-01", TOP("old"))], [("2026-11-20", "no sections")]):
        w = dated_memory.build_window(briefs, "2026-11-22")
        assert w.block == "" and w.days == 0 and w.lines == 0 and w.chars == 0


def test_window_block_starts_with_the_heading_copy_and_chars_counts_it():
    w = dated_memory.build_window([("2026-11-20", TOP("x"))], "2026-11-22")
    assert w.block.startswith("## THE LAST 7 DAYS — ALREADY REPORTED\n")
    assert '("follows from 11-14")' in w.block
    assert w.chars == len(w.block)
    assert w.block.endswith("11-20 [top] x")


def test_window_uses_only_the_first_line_of_a_multiline_entry():
    w = dated_memory.build_window(
        [("2026-11-20", "🌍 TOP STORIES\n• Head line\ncontinuation prose\n")],
        "2026-11-22",
    )
    assert "11-20 [top] Head line" in w.block
    assert "continuation" not in w.block


def test_window_cuts_long_entries_at_200_chars_with_ellipsis():
    w = dated_memory.build_window([("2026-11-20", TOP("w" * 500))], "2026-11-22")
    line = w.block.splitlines()[-1]
    assert len(line) == dated_memory.ENTRY_MAX_CHARS == 200
    assert line.endswith("…") and line.startswith("11-20 [top] w")


def test_window_does_not_cut_a_line_of_exactly_200_chars():
    body = "w" * (200 - len("11-20 [top] "))
    w = dated_memory.build_window([("2026-11-20", TOP(body))], "2026-11-22")
    line = w.block.splitlines()[-1]
    assert line == "11-20 [top] " + body and "…" not in line


def test_window_drops_oldest_days_first_over_the_ceiling():
    entries = [f"{i:03d} " + "z" * 150 for i in range(30)]
    briefs = [(f"2026-11-{d:02d}", TOP(*entries)) for d in (16, 17, 18, 19)]
    w = dated_memory.build_window(briefs, "2026-11-22")
    assert w.chars <= dated_memory.DATED_WINDOW_MAX_CHARS
    assert "11-19 [top]" in w.block
    assert "11-16 [top]" not in w.block
    assert w.chars == len(w.block)
    assert w.days < 4


def test_window_trims_trailing_lines_when_newest_day_alone_is_over():
    entries = [f"{i:03d} " + "z" * 150 for i in range(200)]
    w = dated_memory.build_window([("2026-11-20", TOP(*entries))], "2026-11-22")
    assert w.days == 1
    assert w.chars <= dated_memory.DATED_WINDOW_MAX_CHARS
    assert "11-20 [top] 000 " in w.block
    assert "11-20 [top] 199 " not in w.block
    assert w.lines == len(w.block.splitlines()) - 2


def test_window_orders_top_then_thread_then_watch_within_a_day():
    text = (
        "👁 WATCH / FORWARD\n• w1\n\n🧵 THREADS THAT MOVED\n• t1\n\n"
        "📌 POSITION SIGNALS\n• p1\n\n🌍 TOP STORIES\n• s1\n"
    )
    w = dated_memory.build_window([("2026-11-20", text)], "2026-11-22")
    body = w.block.splitlines()[-3:]
    assert body == ["11-20 [top] s1", "11-20 [thread] t1", "11-20 [watch] w1"]
    assert "p1" not in w.block


def test_window_orders_days_oldest_first_and_counts_days():
    briefs = [("2026-11-18", TOP("b")), ("2026-11-17", TOP("a"))]
    w = dated_memory.build_window(sorted(briefs), "2026-11-22")
    assert w.block.index("11-17") < w.block.index("11-18")
    assert w.days == 2 and w.lines == 2


def test_window_skips_unparseable_brief_dates():
    w = dated_memory.build_window([("not-a-date", TOP("x"))], "2026-11-22")
    assert w.block == ""


def _run_preview(tmp_path, monkeypatch, capsys, files):
    import importlib.util
    import pathlib

    import brief

    monkeypatch.setattr(brief, "BRIEFS_DIR", tmp_path)
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "preview_dated_window", path / "preview_dated_window.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--today", "2026-11-22"]) == 0
    return capsys.readouterr().out


def test_preview_script_reports_files_sections_and_window(
    tmp_path, monkeypatch, capsys
):
    out = _run_preview(
        tmp_path, monkeypatch, capsys, {"brief-2026-11-20.md": TOP("a", "b")}
    )
    assert "files=1" in out and "2026-11-20" in out
    assert "'top': 2" in out
    assert "11-20 [top] a" in out
    assert "days=1 lines=2 chars=" in out


def test_preview_script_distinguishes_no_files_from_no_sections(
    tmp_path, monkeypatch, capsys
):
    empty = _run_preview(tmp_path, monkeypatch, capsys, {})
    assert "files=0" in empty and "NONE RECOGNISED" not in empty
    for f in tmp_path.iterdir():
        f.unlink()
    odd = _run_preview(
        tmp_path, monkeypatch, capsys, {"brief-2026-11-20.md": "prose only"}
    )
    assert "files=1" in odd and "NONE RECOGNISED" in odd
    assert "days=0 lines=0 chars=0" in odd
