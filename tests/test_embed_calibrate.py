"""scripts/embed_calibrate.py (storylines plan 1, task 8).

The metric functions are pure and tested with hand-computed cases. The end-to-end
tests are DB-gated (a skip is not a pass) and use a stub embedder and a stub
judge: nothing here reaches Modal, OpenAI or Anthropic, and nothing here has run
against them. Every text is invented (Widget, Gizmo); the repo is public.
"""

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import common
import db
import group_store as gs
from embeddings import CallRecord

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import embed_calibrate as ec  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (pgvector image).",
)

# ---- pure metrics ----


def test_auc_perfect_and_random():
    assert ec.auc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]) == 1.0
    assert ec.auc([0.1, 0.2, 0.8, 0.9], [True, True, False, False]) == 0.0
    assert ec.auc([0.5, 0.5, 0.5, 0.5], [True, False, True, False]) == 0.5
    # One positive below one negative: 3 of 4 orderings right.
    assert ec.auc([0.9, 0.4, 0.5, 0.1], [True, True, False, False]) == 0.75
    assert math.isnan(ec.auc([0.9, 0.8], [True, True]))


def test_band_weights_restore_the_natural_mix():
    # 90% of the natural 72 h pairs sit in the lowest band, 10% in the top one;
    # the sample has 10 labelled pairs in each.
    natural = {0: 900, 13: 100}
    sims = [0.31] * 10 + [0.97] * 10
    truth = [True] + [False] * 9 + [True] * 9 + [False]
    bands = [ec.band_of(s) for s in sims]
    assert bands == [0] * 10 + [13] * 10

    w = ec.band_weights(bands, natural)

    assert w[0] == pytest.approx(0.9 / 10) and w[-1] == pytest.approx(0.1 / 10)
    p, r, _ = ec.weighted_prf([True] * 20, truth, w)
    # Natural mix: 0.9 * 1/10 + 0.1 * 9/10 = 0.18 of joins are right. The
    # unweighted sample says 10/20 = 0.50, which easy negatives would flatter.
    assert p == pytest.approx(0.18)
    assert r == pytest.approx(1.0)
    # Weights for one space sum to its natural mix over the labelled bands.
    assert sum(w) == pytest.approx(1.0)
    # A pair outside the bands (under 0.30) carries no weight.
    assert ec.band_weights([None, 0], {0: 5}) == [0.0, 1.0]


def test_band_edges():
    assert ec.band_of(0.29) is None
    assert ec.band_of(0.30) == 0
    assert ec.band_of(0.3499) == 0
    assert ec.band_of(0.35) == 1
    assert ec.band_of(0.95) == 13
    assert ec.band_of(1.0) == 13
    assert ec.band_of(1.0000002) == 13  # float noise on a self-pair


def test_join_threshold_is_lowest_with_weighted_precision_95():
    sims = [0.32, 0.33, 0.37, 0.40, 0.40, 0.40, 0.50, 0.55]
    truth = [False, True, False, True, True, True, True, False]
    # The 0.55 negative is nearly weightless; the 0.32 one is heavy.
    w = [5.0, 0.1, 1.0, 1.0, 1.0, 1.0, 1.0, 0.01]
    # 0.30: 4.1 / 10.1; 0.35: 4 / 5.01; 0.40: 4 / 4.01 = 0.9975 >= 0.95.
    assert ec.join_threshold(sims, truth, w) == 0.40
    # Unweighted, no threshold reaches 0.95 (0.40 gives 4/5): the weights decide.
    assert ec.join_threshold(sims, truth, [1.0] * 8) is None
    assert ec.join_threshold([0.5, 0.5], [False, False], [1.0, 1.0]) is None


def test_grid_search_respects_the_precision_floor():
    rows = (
        [(0.85, 0.0, True)] * 5
        + [(0.60, 0.70, True)] * 2
        + [(0.40, 0.50, True)] * 4
        + [(0.40, 0.50, False)] * 2
        + [(0.40, 0.10, False)]
    )
    sims, tsims, truth = (list(x) for x in zip(*rows))
    w = [1.0] * len(rows)

    pair, title, p, r, f1 = ec.grid_search(sims, tsims, truth, w, join=0.80)

    # The best F1 overall is pair 0.40 / title 0.50 (P = 11/13 = 0.846, F1 =
    # 0.917), under the 0.90 floor. The best above it: P = 1, R = 7/11.
    assert p >= 0.90
    assert r == pytest.approx(7 / 11)
    assert f1 == pytest.approx(2 * (7 / 11) / (1 + 7 / 11))
    # Ties go to the stricter pair: highest pair_sim, then highest title_sim.
    assert (pair, title) == (0.60, 0.70)
    assert pair <= 0.80


def test_bar_marks_low_recall_unreliable():
    assert ec.meets_bar(0.90, 0.60)
    assert not ec.meets_bar(0.95, 0.59)
    assert not ec.meets_bar(0.89, 0.99)
    # Through the scorer: precise, but most true pairs sit at 0.32 with titles
    # that never pass, so weighted recall is 0.5 / 0.85 = 0.59.
    rows = [(0.90, 0.0, True)] * 3 + [(0.32, 0.0, True)] * 7 + [(0.32, 0.0, False)] * 3
    score = ec.score_space(rows, natural={0: 100, 12: 100})
    assert score.precision == pytest.approx(1.0)
    assert score.recall == pytest.approx(0.5 / 0.85)
    assert not score.reliable
    assert "grouping unreliable" in score.verdict
    good = ec.score_space(
        [(0.90, 0.0, True)] * 3 + [(0.32, 0.0, False)] * 3, natural={0: 1, 12: 1}
    )
    assert good.reliable and good.join_sim == 0.35


def test_no_join_threshold_is_unreliable():
    score = ec.score_space([(0.5, 0.5, False)] * 4, natural={4: 10})
    assert score.join_sim is None and not score.reliable


def test_ndcg_at_30():
    # Relevant, not, relevant; two relevant in the pool.
    # DCG = 1 + 1/log2(4) = 1.5; ideal = 1 + 1/log2(3).
    expected = 1.5 / (1 + 1 / math.log2(3))
    assert ec.ndcg_at([True, False, True], 2) == pytest.approx(expected)
    assert ec.ndcg_at([True, False, True], 2) == pytest.approx(0.91972, abs=1e-5)
    # A relevant pool item the ranking missed lowers it.
    assert ec.ndcg_at([True], 2) == pytest.approx(1 / (1 + 1 / math.log2(3)))
    # Only the first k count.
    assert ec.ndcg_at([False, True], 1, k=1) == 0.0


def test_bootstrap_interval_is_seeded():
    diffs = [0.1, -0.1, 0.2, 0.0, -0.05]
    a = ec.bootstrap_interval(diffs)
    assert a == ec.bootstrap_interval(diffs)
    assert a[0] <= 0.03 <= a[1]
    assert ec.bootstrap_interval([0.2] * 5) == (pytest.approx(0.2), pytest.approx(0.2))


def test_recommendation_prefers_a_cheaper_gpu_only_when_the_bootstrap_interval_includes_zero():
    big, small = "modal:big@8", "modal:small@8"
    cost = ec.gpu_costs([big, small], [f"{big}=L4", f"{small}=T4"])
    assert cost[small] < cost[big]
    best = [0.9, 0.5, 0.8, 0.6, 0.7, 0.9, 0.4, 0.8]
    close = [0.8, 0.6, 0.8, 0.5, 0.8, 0.8, 0.5, 0.7]  # mean 0.04 lower, noisy
    worse = [x - 0.2 for x in best]

    choice, _ = ec.recommend({big: True, small: True}, {big: best, small: close}, cost)
    assert choice == small
    choice, _ = ec.recommend({big: True, small: True}, {big: best, small: worse}, cost)
    assert choice == big
    # An unreliable space is never recommended, however close.
    choice, _ = ec.recommend({big: True, small: False}, {big: best, small: close}, cost)
    assert choice == big
    # Without --gpu the cheaper space cannot be shown to be cheaper.
    unknown = ec.gpu_costs([big, small], [f"{big}=L4"])
    choice, notes = ec.recommend(
        {big: True, small: True}, {big: best, small: close}, unknown
    )
    assert choice == big
    assert any("tie-break could not apply" in n and small in n for n in notes)
    # OpenAI needs no GPU: it counts as cheapest.
    oa = "openai:text-embedding-3-large@8"
    assert ec.gpu_costs([oa], [])[oa] == 0.0


def test_pairs_are_band_stratified_and_seeded():
    low = {(1000 + i, 2000 + i): 0.31 for i in range(100)}
    high = {(3000 + i, 4000 + i): 0.97 for i in range(5)}
    pools = {
        "s1": {**low, **high, (1, 2): 0.10},  # 0.10 is under every band
        "s2": {**{k: 0.96 for k in high}, **{(5000 + i, 6000): 0.98 for i in range(3)}},
    }

    picked = ec.sample_pairs(pools, 10)

    assert len(picked) == len(set(picked)) == 10
    assert picked == ec.sample_pairs(pools, 10)
    assert sum(p in low for p in picked) == 5  # evenly: five per band
    assert (1, 2) not in picked
    assert ec.sample_pairs(pools, 10, seed=1) != picked  # shuffled, not first-N
    # A thin band gives what it has and the rest fill up to the target.
    more = ec.sample_pairs(pools, 20)
    assert len(more) == 20 and sum(p in low for p in more) == 12


def _tool_resp(name, answers):
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": name, "input": {"answers": answers}}],
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }


def test_judge_labels_map_back_and_unknown_label_is_an_error():
    labels = {"P1": (10, 11), "P2": (12, 13)}
    ok = _tool_resp(
        ec.PAIR_TOOL,
        [{"pair": "P2", "label": "unclear"}, {"pair": "P1", "label": "same"}],
    )
    assert ec.parse_pair_labels(ok, labels) == {(10, 11): "same", (12, 13): "unclear"}

    bad = [
        [{"pair": "P1", "label": "same"}, {"pair": "P3", "label": "same"}],  # unknown
        [{"pair": "P1", "label": "same"}],  # P2 missing
        [{"pair": "P1", "label": "same"}, {"pair": "P1", "label": "same"}],
        [{"pair": "P1", "label": "same"}, {"pair": "P2", "label": "maybe"}],
    ]
    for answers in bad:
        with pytest.raises(ec.JudgeError):
            ec.parse_pair_labels(_tool_resp(ec.PAIR_TOOL, answers), labels)
    with pytest.raises(ec.JudgeError):
        ec.parse_pair_labels({"stop_reason": "max_tokens", "content": []}, labels)
    # The relevance answer maps D labels back to item ids the same way.
    rel = _tool_resp(
        ec.RELEVANCE_TOOL,
        [{"article": "D1", "relevant": True}, {"article": "D2", "relevant": False}],
    )
    assert ec.parse_relevance(rel, {"D1": 7, "D2": 8}) == {7: True, 8: False}


def test_space_specs_parse():
    s = ec.parse_space("modal:Qwen/Qwen3-Embedding-4B@1024=https://x.modal.run/embed")
    assert (s.provider, s.model, s.dim) == ("modal", "Qwen/Qwen3-Embedding-4B", 1024)
    assert s.url == "https://x.modal.run/embed"
    assert s.space == "modal:Qwen/Qwen3-Embedding-4B@1024"
    o = ec.parse_space("openai:text-embedding-3-large@1024")
    assert (o.provider, o.url, o.space) == (
        "openai",
        None,
        "openai:text-embedding-3-large@1024",
    )
    for bad in ("modal:m@1024", "openai:m@1024=https://x", "x:m@1", "modal:m@x=u"):
        with pytest.raises(ValueError):
            ec.parse_space(bad)


def test_openai_space_requires_include_openai(capsys, monkeypatch):
    monkeypatch.setattr(db, "is_configured", lambda: False)
    args = ["--week-ending", "2026-11-23", "--space", "openai:text-embedding-3-small@8"]
    assert ec.main(args, post=_no_post) == 2
    out = capsys.readouterr().out
    assert "--include-openai" in out
    assert ec.main(args + ["--include-openai"], post=_no_post) == 1  # no database
    out = capsys.readouterr().out
    assert "sends one week of titles and summaries" in out
    assert "OpenAI" in out


def _no_post(payload):
    raise AssertionError("the judge was called")


# ---- end to end (DB) ----

WEEK_END = datetime(2026, 11, 23, tzinfo=timezone.utc)
T0 = WEEK_END - timedelta(days=3)
SPACE_A = "modal:fake-a@10"
SPACE_B = "modal:fake-b@10"
SPEC_A = f"{SPACE_A}=https://modal.test/a"
SPEC_B = f"{SPACE_B}=https://modal.test/b"
EVENTS = 4
_ITEM = re.compile(r"item (\d+) event (\d+)")
_QUERY = re.compile(r"about event (\d+)")


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbedder:
    """Event k copies point along axis k (plus a little of axis k+1); a
    follow-up adds its own axis, so it is the same event at about 0.6."""

    provider = "modal"
    dim = 10

    def __init__(self, space, cross):
        self.space, self.cross = space, cross
        self.calls = 0

    def embed(self, texts, *, kind):
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dim
            if kind == "query":
                k = int(_QUERY.search(t).group(1))
                v[k] = 1.0
                v[4 + k] = 0.8
            else:
                n, k = (int(x) for x in _ITEM.search(t).groups())
                v[k] = 1.0
                v[(k + 1) % EVENTS] = self.cross
                v[8 + n % 2] = 0.05 * (1 + n % 3)
                if "follow-up" in t:
                    v[4 + k] = 1.2
            out.append(_unit(v))
        rec = CallRecord(
            "modal",
            self.space,
            len(texts),
            5,
            0.1,
            0.0,
            False,
            False,
            0.01,
            0.0,
            "ok",
            None,
        )
        return out, [rec]


class FakeJudge:
    def __init__(self, same=True, lexical=False):
        self.same, self.lexical = same, lexical
        self.calls = []

    def _query(self, style, k):
        if self.lexical and style == "constraint":
            # Reuses the titles' words, so the lexical arm finds items.
            return f"Widget item event copy about event {k}"
        return f"Gizmo question {style} about event {k}"

    def __call__(self, payload):
        tool = payload["tool_choice"]["name"]
        assert payload["thinking"] == {"type": "disabled"}
        assert [t["name"] for t in payload["tools"]] == [tool]
        text = payload["messages"][0]["content"]
        self.calls.append((tool, text))
        if tool == ec.PAIR_TOOL:
            answers = []
            for lab, a, b in re.findall(r"(P\d+)\nA: (.*)\nB: (.*)", text):
                ka, kb = _ITEM.search(a).group(2), _ITEM.search(b).group(2)
                same = self.same and ka == kb
                answers.append({"pair": lab, "label": "same" if same else "different"})
        elif tool == ec.QUERY_TOOL:
            answers = []
            for lab, body in re.findall(r"(E\d+)\n- (.*)", text):
                k = _ITEM.search(body).group(2)
                answers.append(
                    {
                        "event": lab,
                        **{f: self._query(f, k) for f in ec.QUERY_STYLES},
                    }
                )
        else:
            k = _QUERY.search(text).group(1)
            answers = [
                {"article": lab, "relevant": _ITEM.search(body).group(2) == k}
                for lab, body in re.findall(r"(D\d+): (.*)", text)
            ]
        return _tool_resp(tool, answers)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "_sleep", lambda s: None)
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


def _outlet(conn, name):
    row = conn.execute("SELECT id FROM outlets WHERE name = %s", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _seed(conn):
    """Per event: three copies from three outlets, then a follow-up."""
    ids = {}
    n = 0
    for k in range(EVENTS):
        for j in range(4):
            kind = "follow-up" if j == 3 else "copy"
            title = f"Widget item {n} event {k} {kind}"
            ids[n] = conn.execute(
                "INSERT INTO items (outlet_id, url, title, body, content_hash,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    _outlet(conn, f"Wire {j}"),
                    f"https://example.test/{n}",
                    title,
                    f"Gizmo body text for item {n}",
                    f"h{n}",
                    T0 + timedelta(minutes=10 * n),
                ),
            ).fetchone()[0]
            n += 1
    # One item outside the week is never read.
    conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at)"
        " VALUES (%s, 'https://example.test/old', 'Widget item 99 event 0 copy',"
        " 'hold', %s)",
        (_outlet(conn, "Wire 0"), WEEK_END - timedelta(days=9)),
    )
    conn.commit()
    return ids


def _args(*extra):
    return [
        "--week-ending",
        "2026-11-23",
        "--space",
        SPEC_A,
        "--space",
        SPEC_B,
        "--pairs",
        "40",
        "--gpu",
        f"{SPACE_A}=L4",
        "--gpu",
        f"{SPACE_B}=T4",
        *extra,
    ]


def _embedders():
    return {
        SPACE_A: FakeEmbedder(SPACE_A, 0.6),
        SPACE_B: FakeEmbedder(SPACE_B, 0.5),
    }


def _dir():
    return Path(common.DATA_DIR) / "embed_calibration" / "2026-11-23"


@needs_db
def test_snapshot_and_labels_are_reused_on_rerun(conn, capsys):
    _seed(conn)
    judge = FakeJudge()
    assert ec.main(_args(), embedders=_embedders(), post=judge) == 0
    out = capsys.readouterr().out
    assert "items=16" in out
    first = len(judge.calls)
    assert first > 0
    assert {t for t, _ in judge.calls} == {
        ec.PAIR_TOOL,
        ec.QUERY_TOOL,
        ec.RELEVANCE_TOOL,
    }
    # Every judge call is spent under `calibrate`, committed.
    spend = conn.execute(
        "SELECT count(*), sum(usd) FROM comprehend_spend WHERE stage = 'calibrate'"
    ).fetchone()
    assert spend[0] == first and spend[1] > 0
    # Calibration embeds only the snapshot (the old item has no vector).
    assert conn.execute("SELECT count(*) FROM item_embeddings").fetchone()[0] == 32
    purposes = conn.execute("SELECT DISTINCT purpose FROM embedding_calls").fetchall()
    assert ("calibration",) in purposes
    d = _dir()
    snap = (d / "snapshot.json").read_bytes()

    # A new item in the week after the snapshot does not enter it.
    conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at)"
        " VALUES (%s, 'https://example.test/new', 'Widget item 50 event 1 copy',"
        " 'hnew', %s)",
        (_outlet(conn, "Wire 0"), T0 + timedelta(hours=20)),
    )
    conn.commit()
    embedders = _embedders()
    assert ec.main(_args(), embedders=embedders, post=_no_post) == 0
    out2 = capsys.readouterr().out
    assert "items=16" in out2
    assert (d / "snapshot.json").read_bytes() == snap
    assert all(e.calls == 0 for e in embedders.values())  # vectors and queries cached
    # Same numbers from the same files.
    rows1 = [ln for ln in out.splitlines() if ln.startswith("space=")]
    rows2 = [ln for ln in out2.splitlines() if ln.startswith("space=")]
    assert rows1 == rows2 and len(rows1) == 2


@needs_db
def test_report_never_prints_titles_or_queries(conn, capsys):
    _seed(conn)
    assert ec.main(_args(), embedders=_embedders(), post=FakeJudge()) == 0
    out = capsys.readouterr().out
    for leak in ("Widget", "Gizmo", "example.test", "modal.test", "event 1"):
        assert leak not in out
    assert "recommendation:" in out
    # The files under DATA_DIR hold the text; they are the only place it lives.
    assert "Gizmo question" in (_dir() / "queries.jsonl").read_text("utf-8")


@needs_db
def test_situations_exclude_their_own_items_from_scoring(conn, capsys):
    ids = _seed(conn)
    judge = FakeJudge()
    assert ec.main(_args(), embedders=_embedders(), post=judge) == 0
    out = capsys.readouterr().out
    d = _dir()
    situations = json.loads((d / "situations.json").read_text("utf-8"))["situations"]
    assert len(situations) == EVENTS  # one per event: the copies overlap
    by_id = {v: n for n, v in ids.items()}
    for sit in situations:
        # Own items: the seed and its near-duplicates (the three copies).
        own = {by_id[i] for i in sit["own"]}
        k = by_id[sit["seed"]] // 4
        assert own == {4 * k, 4 * k + 1, 4 * k + 2}
    judged = [t for tool, t in judge.calls if tool == ec.RELEVANCE_TOOL]
    for text in judged:
        k = int(_QUERY.search(text).group(1))
        sent = {int(m) for m in re.findall(r"item (\d+) event", text)}
        assert not sent & {4 * k, 4 * k + 1, 4 * k + 2}
        assert 4 * k + 3 in sent  # the follow-up is in the pool
    # The follow-up ranks first in both spaces: nDCG is 1 over the 12 queries.
    rows = [ln for ln in out.splitlines() if ln.startswith("space=")]
    assert len(rows) == 2
    assert all("ndcg30=1.000" in r and "queries=12" in r for r in rows)


@needs_db
def test_all_unreliable_recommends_none_and_exits_1(conn, capsys):
    _seed(conn)
    assert ec.main(_args(), embedders=_embedders(), post=FakeJudge(same=False)) == 1
    out = capsys.readouterr().out
    assert out.count("grouping unreliable") == 2
    assert "recommendation: none" in out


@needs_db
def test_accept_writes_the_calibration_row(conn, capsys):
    _seed(conn)
    assert gs.latest_calibration(conn, SPACE_B) is None
    rc = ec.main(_args("--accept", SPACE_B), embedders=_embedders(), post=FakeJudge())
    assert rc == 0
    out = capsys.readouterr().out
    cal = gs.latest_calibration(conn, SPACE_B)
    conn.commit()
    assert cal is not None and gs.latest_calibration(conn, SPACE_A) is None
    row = next(ln for ln in out.splitlines() if ln.startswith(f"space={SPACE_B} "))
    assert f"join={cal.join_sim:.2f}" in row and f"pair={cal.pair_sim:.2f}" in row
    assert cal.pair_sim <= cal.join_sim
    assert cal.weighted_precision >= 0.90 and cal.weighted_recall >= 0.60
    assert cal.retrieval_min_sim is None
    assert cal.prompt_version == ec.embeddings.EMBED_PROMPT_VERSION
    assert cal.judge_model == "claude-opus-5-5"
    assert cal.week_ending.isoformat() == "2026-11-23"
    assert cal.pairs_labelled > 0
    for key, value in (
        ("EMBEDDING_PROVIDER", "'modal'"),
        ("EMBEDDING_MODAL_MODEL", "'fake-b'"),
        ("EMBEDDING_DIM", "'10'"),
        ("EMBEDDING_GPU_RATE", "'0.000164'"),
    ):
        assert f"VALUES ('{key}', NULL, {value})" in out
    assert "ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE" in out
    # The Modal URL goes to a file under DATA_DIR, not to the terminal.
    assert "modal.test" not in out
    sql = next(_dir().glob("activate-*.sql")).read_text("utf-8")
    assert "VALUES ('EMBEDDING_MODAL_URL', NULL, 'https://modal.test/b')" in sql


@needs_db
def test_accept_refuses_an_unreliable_space(conn, capsys):
    _seed(conn)
    rc = ec.main(
        _args("--accept", SPACE_A), embedders=_embedders(), post=FakeJudge(same=False)
    )
    assert rc == 1
    assert gs.latest_calibration(conn, SPACE_A) is None
    assert "refused" in capsys.readouterr().out


@needs_db
def test_the_lexical_arm_is_judged_and_its_precision_reported(conn, capsys):
    _seed(conn)
    judge = FakeJudge(lexical=True)
    assert ec.main(_args(), embedders=_embedders(), post=judge) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("lexical:"))
    hits = int(re.search(r"hits=(\d+)", line).group(1))
    assert hits > 0
    # Recompute from the files: lexical hits are pooled and judged.
    judged = {}
    for ln in (_dir() / "relevance.jsonl").read_text("utf-8").splitlines():
        rec = json.loads(ln)
        judged.setdefault(rec["query"], {}).update(rec["judged"])
    constraint = [q for q in judged if q % 3 == 2]
    assert constraint and all(len(judged[q]) > 1 for q in constraint)
    assert re.search(r"precision=0\.\d\d", line)
