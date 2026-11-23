"""Offline attach check (spec 8.2, al7.3 task 7): metrics, verdict and the script.

The metric and verdict tests are pure. The `main` tests are DB-gated (the check
snapshots items from the database): a skip is not a pass. Storylines and
headlines are invented; the repo is public.
"""

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

import common
import db
import storyline_store
import storylines

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import storylines_attach_check as check  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (the check snapshots items)",
)

# ---------------------------------------------------------------- metrics


def test_jaccard_ignores_items_both_left_empty():
    a = {1: {1}, 2: set(), 3: {5}}
    b = {1: {1}, 2: set(), 3: set()}
    mean, n = storylines.jaccard_mean(a, b)
    assert n == 2  # item 2 (both empty) is dropped
    assert mean == pytest.approx(0.5)  # (1.0 + 0.0) / 2


def test_jaccard_only_counts_items_in_both_runs():
    mean, n = storylines.jaccard_mean({1: {1}, 2: {2}}, {1: {1}, 9: {2}})
    assert (mean, n) == (1.0, 1)


def test_jaccard_partial_overlap():
    mean, n = storylines.jaccard_mean({1: {1, 2}}, {1: {2, 3}})
    assert n == 1
    assert mean == pytest.approx(1 / 3)


def test_jaccard_empty_subset_is_nan_and_zero():
    mean, n = storylines.jaccard_mean({1: set()}, {1: set()})
    assert n == 0 and math.isnan(mean)
    mean, n = storylines.jaccard_mean({}, {})
    assert n == 0 and math.isnan(mean)


def test_attach_rate():
    assert storylines.attach_rate({1: {1}, 2: set(), 3: {2, 3}, 4: set()}) == 0.5
    assert storylines.attach_rate({1: set()}) == 0.0
    assert math.isnan(storylines.attach_rate({}))


def test_differ_kinds_less_more_elsewhere():
    h = {1: {1}, 2: {1, 2}, 3: {3}, 4: {1}, 5: {7}}
    s = {1: {1, 2}, 2: {1}, 3: {1}, 4: {1}, 6: {1}}
    # 1: h is a proper subset (less); 2: proper superset (more); 3: disjoint
    # (elsewhere); 4: equal (not counted); 5 and 6 are in one run only.
    assert storylines.differ_kinds(h, s) == {"less": 1, "more": 1, "elsewhere": 1}


def test_differ_kinds_empty_h_is_less_and_empty_s_is_more():
    kinds = storylines.differ_kinds({1: set(), 2: {4}}, {1: {4}, 2: set()})
    assert kinds == {"less": 1, "more": 1, "elsewhere": 0}


def test_differ_kinds_overlapping_but_neither_contains_is_elsewhere():
    assert storylines.differ_kinds({1: {1, 2}}, {1: {2, 3}})["elsewhere"] == 1


def test_verdict_ill_posed_below_065():
    verdict, reasons = storylines.check_verdict(0.64, 0.9, 0.9, 0.9)
    assert verdict == "ILL-POSED"
    assert any("0.64" in r for r in reasons)


def test_verdict_065_is_not_ill_posed():
    assert storylines.check_verdict(0.65, 0.60, 0.5, 0.5)[0] == "PASS"


def test_verdict_nan_j_ss_is_ill_posed():
    assert storylines.check_verdict(float("nan"), 0.9, 0.5, 0.5)[0] == "ILL-POSED"


def test_verdict_pass_uses_the_larger_floor():
    # floor = max(0.70 - 0.10, 0.55) = 0.60
    assert storylines.check_verdict(0.70, 0.60, 0.5, 0.5)[0] == "PASS"
    verdict, reasons = storylines.check_verdict(0.70, 0.59, 0.5, 0.5)
    assert verdict == "FAIL"
    assert any("0.59" in r and "0.60" in r for r in reasons)


def test_verdict_absolute_floor_binds_when_j_ss_is_low():
    # j_ss=0.65 gives j_ss-0.10 = 0.55, which equals the floor; 0.549 fails.
    assert storylines.check_verdict(0.65, 0.55, 0.5, 0.5)[0] == "PASS"
    assert storylines.check_verdict(0.65, 0.549, 0.5, 0.5)[0] == "FAIL"
    # j_ss=0.66: j_ss-0.10 = 0.56 > 0.55, so 0.555 fails. The floor must not
    # be the bare absolute 0.55 here.
    assert storylines.check_verdict(0.66, 0.555, 0.5, 0.5)[0] == "FAIL"


def test_verdict_low_agreement_fails_even_with_a_good_rate():
    assert storylines.check_verdict(0.9, 0.5, 0.9, 0.5)[0] == "FAIL"


def test_verdict_fails_when_haiku_attaches_far_less():
    assert storylines.check_verdict(0.9, 0.9, 0.5 - 0.16, 0.5)[0] == "FAIL"
    assert storylines.check_verdict(0.9, 0.9, 0.5 - 0.14, 0.5)[0] == "PASS"


def test_verdict_reasons_name_each_bar_with_numbers():
    _, reasons = storylines.check_verdict(0.9, 0.9, 0.5, 0.5)
    assert len(reasons) == 3  # ill-posed bar, agreement bar, rate bar
    assert all(re.search(r"\d\.\d\d", r) for r in reasons)


# ----------------------------------------------------------------- script

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-5-5"
WEEK = "2026-11-16"  # items in [2026-11-09, 2026-11-16)
USAGE = {"input_tokens": 100, "output_tokens": 10}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A migrated database with 2 storylines and 5 items inside the week, a data
    dir in tmp_path, and a batch size of 2 (so 3 chunks per run)."""
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "STORYLINE_UPDATE_MODEL", SONNET)
    monkeypatch.setattr(common, "STORYLINE_ATTACH_MODEL", HAIKU)
    monkeypatch.setattr(storylines, "ATTACH_BATCH", 2)
    monkeypatch.setattr(check, "_sleep", lambda s: None)
    conn = db.connect()
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    db.run_migrations(conn)
    conn.commit()
    s1 = storyline_store.add_storyline(
        conn, "Widget tariffs", "Tariff fight", origin="operator", max_live=60
    ).storyline_id
    s2 = storyline_store.add_storyline(
        conn, "Gizmo strike", "Port strike", origin="operator", max_live=60
    ).storyline_id
    conn.commit()
    outlet = conn.execute(
        "INSERT INTO outlets (name) VALUES ('Test Wire') RETURNING id"
    ).fetchone()[0]
    start = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    ids = [
        _insert_item(conn, outlet, n, start + timedelta(hours=n)) for n in range(1, 6)
    ]
    conn.commit()
    yield {
        "conn": conn,
        "tmp": tmp_path,
        "ids": ids,
        "sids": (s1, s2),
        "outlet": outlet,
    }
    conn.close()


def _insert_item(conn, outlet, n, created):
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outlet,
            f"https://example.test/{n}",
            f"Gizmo strike headline {n}",
            f"body {n}",
            f"hash{n}",
            created,
        ),
    ).fetchone()[0]


class Stub:
    """A `post` that answers by rule. rule(model, run_no, n) returns a list of
    storyline labels, None (leave the article unanswered) or an exception to
    raise for the whole call. `run_no` counts the model's complete passes
    (S1 is 0, S2 is 1) on the assumption of 3 chunks per pass."""

    def __init__(self, rule, *, resp_hook=None):
        self.rule = rule
        self.calls = []
        self.resp_hook = resp_hook

    def __call__(self, payload):
        model = payload["model"]
        k = self.calls.count(model)
        self.calls.append(model)
        user = payload["messages"][0]["content"]
        answers = []
        for lab, n in re.findall(r"^(A\d+): Gizmo strike headline (\d+)", user, re.M):
            out = self.rule(model, k // 3, int(n))
            if isinstance(out, BaseException):
                raise out
            if out is None:
                continue
            answers.append({"article": lab, "storylines": out})
        resp = {
            "stop_reason": "tool_use",
            "usage": USAGE,
            "content": [
                {
                    "type": "tool_use",
                    "name": storylines.ATTACH_TOOL,
                    "input": {"answers": answers},
                }
            ],
        }
        return self.resp_hook(resp) if self.resp_hook else resp


def _even_attach(model, run_no, n):
    return ["S1"] if n % 2 == 0 else []


def _run(stub, *extra):
    return check.main(["--week-ending", WEEK, *extra], post=stub)


def _snap(world):
    return world["tmp"] / "storylines_check" / WEEK


def _line_counts(world):
    return {
        p.stem: len(p.read_text().splitlines())
        for p in (_snap(world) / "runs").glob("*.jsonl")
    }


def _sample_lines(out):
    return [x for x in out.splitlines() if x.startswith("sample:")]


@needs_db
def test_main_snapshot_is_reused_and_chunks_resume(world, capsys):
    class Crash(BaseException):
        pass

    stub = Stub(_even_attach)

    def crashing(payload):
        if len(stub.calls) == 4:  # S1's 3 chunks and S2's first are done
            raise Crash()
        return stub(payload)

    with pytest.raises(Crash):
        _run(crashing)
    assert "items=5 storylines=2" in capsys.readouterr().out
    snap_bytes = (_snap(world) / "input.json").read_bytes()
    assert _line_counts(world) == {"S1": 3, "S2": 1}

    # an item created after the snapshot must not enter the second run
    _insert_item(
        world["conn"], world["outlet"], 6, datetime(2026, 11, 10, tzinfo=timezone.utc)
    )
    world["conn"].commit()
    stub2 = Stub(_even_attach)
    code = _run(stub2)
    out = capsys.readouterr().out
    assert "items=5 storylines=2" in out
    assert (_snap(world) / "input.json").read_bytes() == snap_bytes
    # only S2's last 2 chunks and H's 3 are posted, not the 4 already done
    assert stub2.calls == [SONNET, SONNET, HAIKU, HAIKU, HAIKU]
    assert _line_counts(world) == {"S1": 3, "S2": 3, "H": 3}
    assert code == 0
    assert "verdict: PASS" in out


@needs_db
def test_snapshot_rebuilds_storylines_and_holds_only_the_inputs(world):
    world["conn"].execute(
        "UPDATE storylines SET last_attached_at = %s WHERE id = %s",
        (datetime(2026, 11, 8, 9, tzinfo=timezone.utc), world["sids"][0]),
    )
    world["conn"].commit()
    _run(Stub(_even_attach))
    snap = json.loads((_snap(world) / "input.json").read_text())
    rebuilt = [check._storyline_from_json(s) for s in snap["storylines"]]
    assert rebuilt == storyline_store.live_storylines(world["conn"])
    assert rebuilt[0].last_attached_at == datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
    assert rebuilt[1].last_attached_at is None
    assert [i["id"] for i in snap["items"]] == world["ids"]
    assert set(snap["items"][0]) == {"id", "title", "body"}


@needs_db
def test_main_reports_exclusions_per_run_and_warns_when_h_fails_more(world, capsys):
    def rule(model, run_no, n):
        if model == HAIKU and n == 3:
            return ["S99"]  # unknown label: a problem for that article
        if model == SONNET and run_no == 1 and n == 3:
            return ["S99"]
        if model == SONNET and run_no == 1 and n == 4:
            return None  # unanswered
        return _even_attach(model, run_no, n)

    _run(Stub(rule))
    out = capsys.readouterr().out
    # item 3 fails in two runs (counts in both, once in total); item 4 in S2 only
    assert "excluded: S1=0 S2=2 H=1 total=2" in out
    assert "WARNING: H failed share exceeds S1's by more than 2 points" in out


@needs_db
def test_main_no_warning_when_h_fails_no_more_than_s1(world, capsys):
    def rule(model, run_no, n):
        if model == SONNET and run_no == 0 and n == 3:
            return ["S99"]
        return _even_attach(model, run_no, n)

    _run(Stub(rule))
    out = capsys.readouterr().out
    assert "excluded: S1=1 S2=0 H=0 total=1" in out
    assert "WARNING" not in out


@needs_db
def test_main_prints_the_metrics_and_pass_exits_zero(world, capsys):
    code = _run(Stub(_even_attach))
    out = capsys.readouterr().out
    assert code == 0
    assert "J(S1,S2)=1.00 (n=2)" in out  # items 2 and 4 are non-empty
    assert "J(H,S1)=1.00 (n=2)" in out
    assert "attach rate: S1=0.40 S2=0.40 H=0.40" in out
    assert "differ: less=0 more=0 elsewhere=0" in out
    assert "verdict: PASS" in out


@needs_db
def test_main_fail_exits_one_when_haiku_disagrees(world, capsys):
    def rule(model, run_no, n):
        if model == HAIKU:
            return ["S2"] if n % 2 == 0 else []
        return _even_attach(model, run_no, n)

    code = _run(Stub(rule))
    out = capsys.readouterr().out
    assert code == 1
    assert "verdict: FAIL" in out
    assert "differ: less=0 more=0 elsewhere=2" in out


@needs_db
def test_main_ill_posed_when_sonnet_disagrees_with_itself(world, capsys):
    def rule(model, run_no, n):
        if model == SONNET and run_no == 1:
            return ["S2"] if n % 2 == 0 else []
        return _even_attach(model, run_no, n)

    code = _run(Stub(rule))
    out = capsys.readouterr().out
    assert code == 1
    assert "verdict: ILL-POSED" in out


@needs_db
def test_main_samples_are_printed_only(world, capsys):
    def rule(model, run_no, n):
        return [] if model == HAIKU else _even_attach(model, run_no, n)

    _run(Stub(rule), "--sample", "5")
    samples = _sample_lines(capsys.readouterr().out)
    assert len(samples) == 2  # only items 2 and 4 disagree
    assert any(
        "Gizmo strike headline 2" in s and "H=[]" in s and "S1=[Widget tariffs]" in s
        for s in samples
    )
    # only the snapshot and the run logs are written, and the logs hold no titles
    files = sorted(p.name for p in _snap(world).rglob("*") if p.is_file())
    assert files == ["H.jsonl", "S1.jsonl", "S2.jsonl", "input.json"]
    assert not any(
        "headline" in p.read_text() for p in (_snap(world) / "runs").glob("*.jsonl")
    )


@needs_db
def test_main_sample_is_seeded_and_sized(world, capsys):
    def rule(model, run_no, n):
        return [] if model == HAIKU else ["S1"]  # all five disagree

    _run(Stub(rule), "--sample", "3")
    one = _sample_lines(capsys.readouterr().out)
    _run(Stub(rule), "--sample", "3")  # a rerun: all on disk, no posts needed
    two = _sample_lines(capsys.readouterr().out)
    assert len(one) == 3 and one == two
    _run(Stub(rule), "--sample", "0")
    assert _sample_lines(capsys.readouterr().out) == []


@needs_db
def test_main_calls_the_production_builder_and_validator(world, monkeypatch):
    seen = {"build": 0, "validate": 0}
    real_build = storylines.build_attach_request
    real_validate = storylines.validate_attach

    def spy_build(*a, **kw):
        seen["build"] += 1
        return real_build(*a, **kw)

    def spy_validate(*a, **kw):
        seen["validate"] += 1
        return real_validate(*a, **kw)

    monkeypatch.setattr(storylines, "build_attach_request", spy_build)
    monkeypatch.setattr(storylines, "validate_attach", spy_validate)
    _run(Stub(_even_attach))
    assert seen == {"build": 9, "validate": 9}


@needs_db
def test_main_records_spend_per_response_as_attach_check(world):
    _run(Stub(_even_attach))
    rows = (
        world["conn"]
        .execute(
            "SELECT stage, model, count(*) FROM comprehend_spend"
            " GROUP BY 1, 2 ORDER BY 2"
        )
        .fetchall()
    )
    assert rows == [("attach_check", HAIKU, 3), ("attach_check", SONNET, 6)]


@needs_db
def test_main_transient_error_retries_then_succeeds(world, monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(check, "_sleep", sleeps.append)
    fails = {"n": 0}

    def rule(model, run_no, n):
        if fails["n"] < 2:
            fails["n"] += 1
            return requests.ConnectionError("down")
        return _even_attach(model, run_no, n)

    code = _run(Stub(rule))
    assert sleeps == [30, 30]
    assert code == 0
    assert "excluded: S1=0 S2=0 H=0 total=0" in capsys.readouterr().out


@needs_db
def test_main_exhausted_transient_leaves_the_chunk_unrecorded(
    world, monkeypatch, capsys
):
    sleeps = []
    monkeypatch.setattr(check, "_sleep", sleeps.append)
    fails = {"n": 0}

    def rule(model, run_no, n):
        if model == SONNET and fails["n"] < 3:
            fails["n"] += 1
            return requests.Timeout("slow")
        return _even_attach(model, run_no, n)

    _run(Stub(rule))
    out = capsys.readouterr().out
    assert sleeps == [30, 30]  # three attempts, a pause between each
    assert "chunk 0 of S1 incomplete" in out
    lines = (_snap(world) / "runs" / "S1.jsonl").read_text().splitlines()
    assert [json.loads(x)["chunk"] for x in lines] == [1, 2]
    # the unrecorded chunk's two items count as excluded for S1
    assert "excluded: S1=2 " in out


@needs_db
def test_main_non_transient_error_marks_every_article_a_problem(
    world, monkeypatch, capsys
):
    sleeps = []
    monkeypatch.setattr(check, "_sleep", sleeps.append)
    resp = requests.Response()
    resp.status_code = 400
    err = requests.HTTPError("bad request", response=resp)

    def rule(model, run_no, n):
        return err if model == HAIKU and n in (1, 2) else _even_attach(model, run_no, n)

    _run(Stub(rule))
    out = capsys.readouterr().out
    assert sleeps == []  # not retried
    assert "excluded: S1=0 S2=0 H=2 total=2" in out
    rec = [
        json.loads(x)
        for x in (_snap(world) / "runs" / "H.jsonl").read_text().splitlines()
    ]
    first = next(r for r in rec if r["chunk"] == 0)
    assert first["decided"] == {}
    assert set(first["problems"]) == {str(i) for i in world["ids"][:2]}


@needs_db
def test_main_whole_result_marks_every_article_a_problem(world, capsys):
    calls = {"n": 0}

    def hook(resp):
        calls["n"] += 1
        if calls["n"] == 7:  # H's first chunk
            return {"stop_reason": "max_tokens", "usage": USAGE, "content": []}
        return resp

    _run(Stub(_even_attach, resp_hook=hook))
    assert "excluded: S1=0 S2=0 H=2 total=2" in capsys.readouterr().out
    # the unusable response was still paid for
    n = (
        world["conn"]
        .execute("SELECT count(*) FROM comprehend_spend WHERE stage = 'attach_check'")
        .fetchone()[0]
    )
    assert n == 9


@needs_db
def test_main_unpriced_model_exits_two_before_any_call(world, monkeypatch, capsys):
    monkeypatch.setattr(common, "STORYLINE_ATTACH_MODEL", "no-such-model")
    stub = Stub(_even_attach)
    assert _run(stub) == 2
    assert stub.calls == []
    assert "no-such-model" in capsys.readouterr().out
    assert not _snap(world).exists()


@needs_db
def test_main_empty_week_exits_two_and_writes_no_snapshot(world):
    stub = Stub(_even_attach)
    assert check.main(["--week-ending", "2026-01-05"], post=stub) == 2
    assert stub.calls == []
    assert not (world["tmp"] / "storylines_check" / "2026-01-05").exists()


def test_main_bad_usage_exits_two(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    assert check.main(["--week-ending", "not-a-date"], post=lambda p: {}) == 2
    assert check.main([], post=lambda p: {}) == 2


def test_default_post_posts_with_one_attempt(monkeypatch):
    import brief

    seen = {}

    def fake(payload, **kw):
        seen.update(kw)
        return {"ok": 1}

    monkeypatch.setattr(brief, "_post_messages", fake)
    assert check._default_post({"model": "m"}) == {"ok": 1}
    assert seen == {
        "timeout": brief.ATTACH_TIMEOUT,
        "max_attempts": 1,
        "label": "AttachCheck",
    }


@needs_db
def test_resume_survives_a_truncated_last_line(world, capsys):
    _run(Stub(_even_attach))
    capsys.readouterr()
    path = _snap(world) / "runs" / "H.jsonl"
    lines = path.read_text().splitlines()
    # a crash mid-append: chunk 2 is cut short, with no trailing newline
    path.write_text("\n".join(lines[:2]) + "\n" + lines[2][:25])
    stub = Stub(_even_attach)
    assert _run(stub) == 0
    assert stub.calls == [HAIKU]  # only the cut chunk is redone
    # the fragment is closed onto its own line, not glued to the new record
    assert path.read_text().splitlines()[2] == lines[2][:25]
    assert check._read_run(path).keys() == {0, 1, 2}
    assert "verdict: PASS" in capsys.readouterr().out


@needs_db
def test_snapshot_is_written_through_a_temp_file(world, monkeypatch):
    def boom(self, target):
        raise OSError("killed before the rename")

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", boom)
        with pytest.raises(OSError):
            _run(Stub(_even_attach))
    assert not (_snap(world) / "input.json").exists()
    # a leftover temp file from the crash is ignored and overwritten
    assert _run(Stub(_even_attach)) in (0, 1)
    assert json.loads((_snap(world) / "input.json").read_text())["items"]
