"""Schema tests for migration 0020 (pgvector and the groups tables, plan 1 task 1).

DB-gated: a skip is not a pass. All text is invented; the repo is public.
"""

from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

import conftest
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test "
    "pgvector/pgvector:0.8.7-pg18-trixie",
)

TARGET = "0020_groups"
UP_FILE = Path(__file__).resolve().parent.parent / "migrations" / "0020_groups_up.sql"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
TABLES = (
    "item_embeddings",
    "item_embed_skips",
    "query_embeddings",
    "embedding_calls",
    "embedding_calibrations",
    "item_groups",
    "item_group_members",
)


@pytest.fixture()
def conn():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


def _item(conn, n=1):
    row = conn.execute("SELECT id FROM outlets WHERE name = 'Test Wire'").fetchone()
    outlet = (
        row[0]
        if row
        else conn.execute(
            "INSERT INTO outlets (name) VALUES ('Test Wire') RETURNING id"
        ).fetchone()[0]
    )
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outlet,
            f"https://example.test/{n}",
            f"Widget headline {n}",
            "body",
            f"h{n}",
            NOW,
        ),
    ).fetchone()[0]


def _embed(conn, item_id, space, dim, vec):
    conn.execute(
        "INSERT INTO item_embeddings (item_id, space, text_hash, dim, embedding)"
        " VALUES (%s, %s, 'th', %s, %s::vector)",
        (item_id, space, dim, vec),
    )


def _down(conn):
    return db.run_migrations(
        conn, direction="down", steps=conftest.steps_back_through(conn, TARGET)
    )


def test_vector_extension_is_installed(store):
    rows = store.execute(
        "SELECT extname FROM pg_extension WHERE extname = 'vector'"
    ).fetchall()
    assert len(rows) == 1
    sim = store.execute("SELECT 1 - ('[1,0]'::vector <=> '[0,1]'::vector)").fetchone()[
        0
    ]
    assert sim == 0.0


def test_embedding_dimension_must_match(store):
    item = _item(store)
    store.commit()  # the rollback below must not take the item with it
    with pytest.raises(psycopg.errors.CheckViolation):
        _embed(store, item, "a@3", 3, "[1,0]")
    store.rollback()
    # The control: the same row with a matching dimension inserts.
    _embed(store, item, "a@3", 3, "[1,0,0]")


def test_two_spaces_of_different_dimensions_coexist(store):
    item = _item(store)
    _embed(store, item, "a@2", 2, "[1,0]")
    _embed(store, item, "b@3", 3, "[1,0,0]")
    n = store.execute(
        "SELECT count(*) FROM item_embeddings WHERE item_id = %s", (item,)
    ).fetchone()[0]
    assert n == 2


def _calibration(conn, join_sim, pair_sim):
    conn.execute(
        "INSERT INTO embedding_calibrations"
        " (space, join_sim, pair_sim, title_sim, prompt_version)"
        " VALUES ('a@2', %s, %s, 0.5, 1)",
        (join_sim, pair_sim),
    )


def test_calibration_rejects_pair_above_join(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        _calibration(store, 0.8, 0.9)
    store.rollback()
    _calibration(store, 0.9, 0.8)  # control: pair <= join is accepted


def test_down_refuses_while_rows_exist(store):
    store.execute(
        "INSERT INTO embedding_calls (provider, space, purpose, inputs, tokens, usd,"
        " openai_equivalent_usd, outcome)"
        " VALUES ('modal', 'a@2', 'items', 1, 10, 0.001, 0.001, 'ok')"
    )
    store.commit()
    with pytest.raises(Exception, match="embedding_calls has rows"):
        _down(store)
    store.rollback()
    for table in TABLES:
        assert store.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0], table


def test_guard_message_names_the_image_swap():
    text = UP_FILE.read_text(encoding="utf-8")
    assert "pgvector is not installed in this Postgres image" in text
    assert "Rollout step 0" in text
    assert text.index("RAISE EXCEPTION") < text.index("CREATE EXTENSION")


def test_down_then_up_round_trips_on_an_empty_schema(store):
    _down(store)
    for table in TABLES:
        assert store.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None
    db.run_migrations(store)
    store.commit()
    assert store.execute(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    store.execute(
        "INSERT INTO comprehend_spend (stage, model, input_tokens, output_tokens, usd)"
        " VALUES ('calibrate', 'm', 1, 1, 0.01)"
    )
