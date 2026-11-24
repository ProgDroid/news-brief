"""scripts/embed_backfill.py (storylines plan 1, task 7).

DB-gated: a skip is not a pass. Text is invented (Widget, Gizmo); the repo is
public. OpenAI is a stub: nothing here reaches the network, and nothing here has
run against the real Batch API.
"""

import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import common
import db
import embeddings
import group_store as gs
import grouping
from embeddings import CallRecord, EmbedError, embed_text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import embed_backfill as eb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (pgvector image).",
)

DIM = 3
SPACE = "modal:fake@3"
OPENAI_MODEL = "text-embedding-3-large"
OPENAI_SPACE = f"openai:{OPENAI_MODEL}@3"
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
A = [1.0, 0.0, 0.0]
A2 = [0.99, 0.1, 0.0]
B = [0.0, 1.0, 0.0]


@pytest.fixture()
def conn():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    return tmp_path


def _item(conn, n, title=None, body=None):
    row = conn.execute("SELECT id FROM outlets WHERE name = 'Test Wire'").fetchone()
    outlet = (
        row[0]
        if row
        else conn.execute(
            "INSERT INTO outlets (name, kind) VALUES ('Test Wire', 'wire') RETURNING id"
        ).fetchone()[0]
    )
    row = conn.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outlet,
            f"https://example.test/{n}",
            title or f"Widget {n} output changes",
            body,
            f"h{n}",
            T0 + timedelta(minutes=n),
        ),
    ).fetchone()
    conn.commit()
    return row[0]


def _text(conn, item_id):
    t, b, u = conn.execute(
        "SELECT title, body, url FROM items WHERE id = %s", (item_id,)
    ).fetchone()
    return embed_text(t, b, u)


def _calibrate(conn, space):
    gs.write_calibration(
        conn,
        gs.Calibration(
            space=space,
            join_sim=0.85,
            pair_sim=0.7,
            title_sim=0.5,
            retrieval_min_sim=None,
            weighted_precision=0.9,
            weighted_recall=0.8,
            pairs_labelled=200,
            prompt_version=1,
            judge_model="judge-m",
            week_ending=date(2026, 11, 22),
        ),
    )
    conn.commit()


def _count(conn, table, where="TRUE", args=()):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[
        0
    ]


def _hash_vec(text):
    d = hashlib.sha256(text.encode()).digest()
    v = [d[i] - 127.5 for i in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbedder:
    provider = "modal"
    dim = DIM

    def __init__(self, vectors=None, space=SPACE, error=None):
        self.space = space
        self.vectors = dict(vectors or {})
        self.error = error
        self.sent = []

    def embed(self, texts, *, kind):
        self.sent.append(list(texts))
        if self.error:
            raise EmbedError(self.error, "boom at https://modal.test/x?k=s3cret", [])
        vecs = [self.vectors.get(t) or _hash_vec(t) for t in texts]
        rec = CallRecord(
            "modal",
            self.space,
            len(texts),
            10,
            0.1,
            0.0,
            False,
            False,
            0.25,
            0.1,
            "ok",
            None,
        )
        return vecs, [rec]


MODAL_ARGS = ["--provider", "modal", "--model", "fake", "--dim", "3"]


# ---- the synchronous path ----


def test_modal_backfill_embeds_then_groups_and_prints_totals(conn, monkeypatch, capsys):
    a, a2, b = _item(conn, 1), _item(conn, 2), _item(conn, 3)
    emb = FakeEmbedder({_text(conn, a): A, _text(conn, a2): A2, _text(conn, b): B})
    _calibrate(conn, SPACE)
    monkeypatch.setattr(eb, "ROUND_ITEMS", 2)  # three items: two rounds

    rc = eb.main(MODAL_ARGS, embedder=emb)

    out = capsys.readouterr().out
    assert rc == 0
    assert _count(conn, "item_embeddings", "space = %s", (SPACE,)) == 3
    rounds = [ln for ln in out.splitlines() if ln.startswith("round ")]
    assert len(rounds) == 2
    assert "embedded=2" in rounds[0] and "usd=0.2500" in rounds[0]
    assert "embedded=1" in rounds[1]
    # A and A2 are close enough to join; B opens its own group.
    assert "grouped=3 opened=2 joined=1" in out
    assert _count(conn, "item_groups") == 2
    # Counts only: no title, no URL in what was printed.
    assert "Widget" not in out and "example.test" not in out


def test_no_group_embeds_only(conn, capsys):
    _item(conn, 1)
    _calibrate(conn, SPACE)
    assert eb.main(MODAL_ARGS + ["--no-group"], embedder=FakeEmbedder()) == 0
    assert _count(conn, "item_embeddings") == 1
    assert _count(conn, "item_groups") == 0
    assert "grouped=" not in capsys.readouterr().out


def test_an_uncalibrated_space_is_embedded_and_says_so(conn, capsys):
    _item(conn, 1)
    assert eb.main(MODAL_ARGS, embedder=FakeEmbedder()) == 0
    assert _count(conn, "item_embeddings") == 1
    assert _count(conn, "item_groups") == 0
    assert f"uncalibrated: {SPACE}" in capsys.readouterr().out


def test_a_provider_error_exits_1_and_prints_no_url(conn, capsys):
    _item(conn, 1)
    rc = eb.main(MODAL_ARGS, embedder=FakeEmbedder(error="config"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "config" in out and "boom" in out
    assert "modal.test" not in out and "s3cret" not in out
    assert _count(conn, "item_embeddings") == 0


def test_backfill_into_a_non_active_space_leaves_the_active_space_untouched(
    conn, monkeypatch
):
    active = "modal:active@3"
    monkeypatch.setattr(common, "EMBEDDING_PROVIDER", "modal")
    ids = [_item(conn, 1), _item(conn, 2)]
    # The active space already has its vectors and groups.
    _calibrate(conn, active)
    assert (
        eb.main(
            ["--provider", "modal", "--model", "active", "--dim", "3"],
            embedder=FakeEmbedder(space=active),
        )
        == 0
    )
    before = (
        _count(conn, "item_embeddings", "space = %s", (active,)),
        _count(conn, "item_group_members", "space = %s", (active,)),
        _count(conn, "item_groups", "space = %s", (active,)),
    )
    assert before[0] == 2 and before[1] == 2
    _item(conn, 3)  # new item: the active space has not seen it

    other = FakeEmbedder(space="modal:new@3")
    assert (
        eb.main(["--provider", "modal", "--model", "new", "--dim", "3"], embedder=other)
        == 0
    )

    assert _count(conn, "item_embeddings", "space = %s", ("modal:new@3",)) == 3
    assert _count(conn, "item_embeddings", "space = %s", (active,)) == before[0]
    assert _count(conn, "item_group_members", "space = %s", (active,)) == before[1]
    assert _count(conn, "item_groups", "space = %s", (active,)) == before[2]
    assert ids  # the earlier items exist in both spaces


def test_backfill_exits_4_when_the_space_lock_is_held(conn, capsys):
    _item(conn, 1)
    other = db.connect()
    try:
        with grouping.space_lock(other, SPACE) as got:
            assert got
            rc = eb.main(MODAL_ARGS, embedder=FakeEmbedder())
        assert rc == 4
        assert f"busy: another pass holds {SPACE}" in capsys.readouterr().out
        assert _count(conn, "item_embeddings") == 0
        # Control: with the lock released, the same call runs.
        assert eb.main(MODAL_ARGS, embedder=FakeEmbedder()) == 0
        assert _count(conn, "item_embeddings") == 1
    finally:
        other.close()


def test_usage_errors_exit_2(conn, capsys):
    assert eb.main([], embedder=FakeEmbedder()) == 2
    assert eb.main(["--provider", "modal"], embedder=FakeEmbedder()) == 2
    # --openai-batch with a modal embedder.
    assert eb.main(MODAL_ARGS + ["--openai-batch"], embedder=FakeEmbedder()) == 2


# ---- the OpenAI Batch API (a stub; the real one was not run) ----


class Resp:
    def __init__(self, code=200, body=None, text=""):
        self.status_code = code
        self._body = body
        self.text = text if body is None else json.dumps(body)

    def json(self):
        return self._body


class StubOpenAI:
    """Records every request. `statuses` is the list of batch statuses returned
    in turn (the last one repeats); the output file echoes each input chunk as
    a hash-based embedding, lines and `data` both in reverse order."""

    def __init__(self, statuses=("completed",), fail_chunks=(), per_status=None):
        self.statuses = list(statuses)
        self.fail_chunks = set(fail_chunks)
        self.per_status = per_status  # batch id -> status list
        self.posts, self.gets = [], []
        self.inputs = {}  # input file id -> parsed request lines
        self.batches = {}  # batch id -> input file id
        self.polls = {}

    def post(self, url, **kw):
        self.posts.append(url)
        assert kw["headers"]["Authorization"] == "Bearer sk-test-not-real"
        if url.endswith("/files"):
            assert kw["data"] == {"purpose": "batch"}
            data = kw["files"]["file"][1]
            fid = f"file-in-{len(self.inputs)}"
            self.inputs[fid] = [json.loads(x) for x in data.decode().splitlines()]
            return Resp(body={"id": fid})
        assert url.endswith("/batches")
        assert kw["json"]["endpoint"] == "/v1/embeddings"
        assert kw["json"]["completion_window"] == "24h"
        bid = f"batch-{len(self.batches)}"
        self.batches[bid] = kw["json"]["input_file_id"]
        return Resp(body={"id": bid})

    def get(self, url, **kw):
        self.gets.append(url)
        if "/batches/" in url:
            bid = url.rsplit("/", 1)[1]
            seq = (self.per_status or {}).get(bid, self.statuses)
            n = self.polls.get(bid, 0)
            self.polls[bid] = n + 1
            status = seq[min(n, len(seq) - 1)]
            return Resp(
                body={
                    "id": bid,
                    "status": status,
                    "output_file_id": f"out-{bid}" if status == "completed" else None,
                    "error_file_id": f"err-{bid}",
                }
            )
        if url.endswith("/content"):
            fid = url.split("/files/")[1].rsplit("/", 1)[0]
            if fid.startswith("err-"):
                return Resp(text="{}\n{}\n{}\n")
            bid = fid[len("out-") :]
            return Resp(text=self._output(self.inputs[self.batches[bid]]))
        raise AssertionError(url)

    @staticmethod
    def vec(text):
        return _hash_vec(text)

    def _output(self, lines):
        out = []
        for ln in reversed(lines):
            cid, inp = ln["custom_id"], ln["body"]["input"]
            assert ln["method"] == "POST" and ln["url"] == "/v1/embeddings"
            assert ln["body"]["dimensions"] == DIM
            if cid in self.fail_chunks:
                out.append(
                    {
                        "custom_id": cid,
                        "response": None,
                        "error": {"code": "x", "message": "bad"},
                    }
                )
                continue
            data = [
                {"index": i, "embedding": [x * 3 for x in self.vec(t)]}
                for i, t in enumerate(inp)
            ]
            tokens = sum(len(t) // 4 + 1 for t in inp)
            out.append(
                {
                    "custom_id": cid,
                    "response": {
                        "status_code": 200,
                        "body": {
                            "data": list(reversed(data)),
                            "usage": {"prompt_tokens": tokens},
                        },
                    },
                    "error": None,
                }
            )
        return "\n".join(json.dumps(x) for x in out) + "\n"


OPENAI_ARGS = [
    "--provider",
    "openai",
    "--model",
    OPENAI_MODEL,
    "--dim",
    "3",
    "--openai-batch",
]


def _state(data_dir):
    (path,) = list((data_dir / "embed_backfill").glob("*/state.json"))
    return path, json.loads(path.read_text())


def test_openai_batch_submits_once_and_resumes_polling(conn, data_dir, capsys):
    for n in range(1, 4):
        _item(conn, n)
    api = StubOpenAI(statuses=["in_progress"])
    sleeps = []

    rc = eb.main(
        OPENAI_ARGS + ["--wait", "0"], post=api.post, get=api.get, sleep=sleeps.append
    )

    assert rc == 3
    assert len(api.posts) == 2  # one upload, one batch
    path, state = _state(data_dir)
    assert [b["id"] for b in state["batches"]] == ["batch-0"]
    assert _count(conn, "item_embeddings") == 0
    assert "rerun later" in capsys.readouterr().out
    assert sleeps == []

    # The rerun finds state.json and polls; it does not upload again.
    api.statuses = ["in_progress", "completed"]
    api.polls.clear()
    rc = eb.main(
        OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get, sleep=sleeps.append
    )

    assert rc == 0
    assert len(api.posts) == 2
    assert sleeps == [eb.POLL_INTERVAL_S]
    assert _count(conn, "item_embeddings", "space = %s", (OPENAI_SPACE,)) == 3
    assert _state(data_dir)[1]["batches"][0]["done"] is True
    # A third run has nothing missing and submits nothing.
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get) == 0
    assert len(api.posts) == 2


def test_openai_batch_splits_above_the_input_cap(conn, data_dir, monkeypatch):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    monkeypatch.setattr(eb, "BATCH_MAX_INPUTS", 4)
    for n in range(1, 7):
        _item(conn, n)
    api = StubOpenAI(statuses=["in_progress"])

    rc = eb.main(
        OPENAI_ARGS + ["--wait", "0", "--max-items", "6"], post=api.post, get=api.get
    )

    assert rc == 3
    _, state = _state(data_dir)
    assert [b["inputs"] for b in state["batches"]] == [4, 2]
    assert [b["id"] for b in state["batches"]] == ["batch-0", "batch-1"]
    sizes = [
        sum(len(ln["body"]["input"]) for ln in lines) for lines in api.inputs.values()
    ]
    assert sizes == [4, 2]
    assert len(api.posts) == 4  # two uploads, two batches


def test_openai_batch_output_maps_back_by_custom_id_and_is_priced_at_half(
    conn, data_dir, monkeypatch
):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    # Long bodies: the usd column keeps six places, so a few tokens would round to 0.
    ids = [_item(conn, n, body=f"Gizmo plant {n} " * 60) for n in range(1, 5)]
    api = StubOpenAI()

    rc = eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get)

    assert rc == 0
    for iid in ids:
        lit = conn.execute(
            "SELECT embedding::text FROM item_embeddings WHERE item_id = %s", (iid,)
        ).fetchone()[0]
        got = embeddings.from_pg_vector(lit)
        want = _hash_vec(_text(conn, iid))
        assert got == pytest.approx(want, abs=1e-5)  # normalised, in its own slot
    calls = conn.execute(
        "SELECT inputs, tokens, batch, usd, openai_equivalent_usd, purpose, outcome"
        " FROM embedding_calls ORDER BY id"
    ).fetchall()
    assert [c[0] for c in calls] == [2, 2]  # one call per chunk
    price = embeddings.OPENAI_PRICES_PER_MTOK[OPENAI_MODEL]
    for inputs, tokens, batch, usd, equiv, purpose, outcome in calls:
        assert batch is True and purpose == "backfill" and outcome == "ok"
        assert float(usd) == pytest.approx(tokens * price / 1e6 * 0.5, abs=1e-6)
        assert float(equiv) == pytest.approx(tokens * price / 1e6, abs=1e-6)
        assert tokens > 200  # so half and full price differ by more than the rounding
        assert tokens > 0


def test_a_failed_line_leaves_its_items_unembedded_and_a_rerun_resubmits_only_them(
    conn, data_dir, monkeypatch, capsys
):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    for n in range(1, 5):
        _item(conn, n)
    api = StubOpenAI(fail_chunks={"chunk-1"})

    rc = eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get)

    out = capsys.readouterr().out
    assert rc == 1  # two items are still missing: never "done"
    assert "failed_lines=1" in out and "missing=2" in out
    assert _count(conn, "item_embeddings") == 2
    failed = conn.execute(
        "SELECT outcome, usd FROM embedding_calls WHERE outcome = 'failed'"
    ).fetchall()
    assert failed == [("failed", 0)]

    api2 = StubOpenAI()
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api2.post, get=api2.get) == 0
    (lines,) = api2.inputs.values()
    assert sum(len(ln["body"]["input"]) for ln in lines) == 2  # only the two missing
    assert _count(conn, "item_embeddings") == 4


def test_a_failed_batch_exits_1_keeps_state_and_prints_counts_only(
    conn, data_dir, capsys
):
    _item(conn, 1)
    api = StubOpenAI(statuses=["failed"])

    rc = eb.main(OPENAI_ARGS, post=api.post, get=api.get)

    out = capsys.readouterr().out
    assert rc == 1
    assert "status=failed" in out and "error_lines=3" in out
    path, state = _state(data_dir)
    assert path.exists() and state["batches"] == []
    (old,) = state["history"]
    assert old["status"] == "failed" and old["id"] == "batch-0" and old["seen_at"]
    assert _count(conn, "item_embeddings") == 0


def test_refuses_above_max_usd(conn, data_dir, capsys):
    _item(conn, 1, body="Gizmo " * 100)
    api = StubOpenAI()

    rc = eb.main(OPENAI_ARGS + ["--max-usd", "0.0000001"], post=api.post, get=api.get)

    assert rc == 2
    assert api.posts == [] and api.gets == []
    assert "refused" in capsys.readouterr().out
    # The control: the same item under the default cap goes through.
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get) == 0
    assert len(api.posts) == 2


def test_an_openai_http_error_exits_1_without_the_body_url(conn):
    _item(conn, 1)

    def post(url, **kw):
        return Resp(401, text="no access https://api.openai.com/v1/files nope")

    assert eb.main(OPENAI_ARGS, post=post, get=lambda *a, **k: None) == 1
    assert _count(conn, "item_embeddings") == 0


def test_space_slug_keeps_only_safe_characters():
    assert eb.space_slug("openai:text-embedding-3-large@1024") == (
        "openai_text-embedding-3-large_1024"
    )
    assert eb.space_slug("a/b c") == "a_b_c"


def test_a_failed_batch_is_retired_and_the_next_run_resubmits_what_is_missing(
    conn, data_dir, monkeypatch
):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    for n in range(1, 4):
        _item(conn, n)
    api = StubOpenAI(statuses=["failed"])
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get) == 1
    assert len(api.posts) == 2

    api2 = StubOpenAI()
    rc = eb.main(OPENAI_ARGS + ["--no-group"], post=api2.post, get=api2.get)

    assert rc == 0
    (lines,) = api2.inputs.values()  # a new upload...
    assert sum(len(ln["body"]["input"]) for ln in lines) == 3  # ...for the missing
    assert _count(conn, "item_embeddings") == 3
    _, state = _state(data_dir)
    assert len(state["history"]) == 1  # the history survives the fresh state


def test_a_run_with_items_still_missing_never_exits_0(
    conn, data_dir, monkeypatch, capsys
):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    for n in range(1, 5):
        _item(conn, n)
    api = StubOpenAI(fail_chunks={"chunk-0"})
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get) == 1
    assert "missing=2" in capsys.readouterr().out
    # An explicit --max-items cap is the operator's own limit: it only reports.
    api2 = StubOpenAI(fail_chunks={"chunk-0"})
    rc = eb.main(
        OPENAI_ARGS + ["--no-group", "--max-items", "2"], post=api2.post, get=api2.get
    )
    assert rc == 0 and "missing=" in capsys.readouterr().out
    # Control: once everything is embedded the same call is 0 and prints no count.
    api3 = StubOpenAI()
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api3.post, get=api3.get) == 0
    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api3.post, get=api3.get) == 0
    assert "missing=" not in capsys.readouterr().out


def test_a_rerun_after_a_crash_after_commit_does_not_double_record_spend(
    conn, data_dir, monkeypatch
):
    monkeypatch.setattr(eb, "BATCH_CHUNK", 2)
    for n in range(1, 5):
        _item(conn, n)
    api = StubOpenAI()
    real = eb._save_state

    def crash_once_a_batch_is_done(path, state):
        if any(b["done"] for b in state["batches"]):
            raise RuntimeError("crash after the commit")
        real(path, state)

    monkeypatch.setattr(eb, "_save_state", crash_once_a_batch_is_done)
    with pytest.raises(RuntimeError):
        eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get)
    assert _count(conn, "item_embeddings") == 4  # committed
    assert _count(conn, "embedding_calls") == 2
    monkeypatch.setattr(eb, "_save_state", real)

    assert eb.main(OPENAI_ARGS + ["--no-group"], post=api.post, get=api.get) == 0
    assert _count(conn, "embedding_calls") == 2  # one row per chunk, not four
    assert len(api.posts) == 2  # and nothing was resubmitted
