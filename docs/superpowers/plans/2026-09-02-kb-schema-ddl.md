# KB Schema DDL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship migration `0006_knowledge_base` — sixteen tables, sixteen indexes and one immutability trigger for the knowledge base, with a test per constraint.

**Architecture:** One up/down SQL pair in `migrations/`, applied by the existing runner. No Python module is added, so no `Dockerfile` or workflow edit is needed. Tables ship empty; nothing reads or writes them until `bqa.4`. All new tests live in one new file, `tests/test_kb_schema.py`, with its own module-local `conn` fixture — matching the pattern already used by `tests/test_db.py:38` and `tests/test_config.py:38`.

**Tech Stack:** PostgreSQL 18 (`postgres:18-alpine`), psycopg 3, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-kb-schema-ddl-design.md`

## Global Constraints

- **PostgreSQL 15+** for `NULLS NOT DISTINCT`. The stack is on 18. Do not substitute paired partial unique indexes.
- **No `pgvector`**, no `CREATE EXTENSION` (`news-brief-bqa.7`).
- **No `docker-compose.yml` change** — the repo copy has drifted behind the host's (`news-brief-qx4`).
- **No change to `sources`, `brief_memory.py`, `brief_memory.json`, or `tests/conftest.py`.** The cutover is `bqa.4` / `news-brief-bqa.9`.
- **File names:** `0006_knowledge_base_up.sql`, `0006_knowledge_base_down.sql`. The runner derives the version `0006_knowledge_base` from the filename.
- **Every table carries** `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, join tables included.
- **`ON DELETE RESTRICT` raises `psycopg.errors.RestrictViolation` (SQLSTATE 23001), NOT `ForeignKeyViolation` (23503)** — and `RestrictViolation` is *not* a subclass of it, so a test expecting the wrong one errors out rather than passing. Verified against psycopg 3.3.4 and a live PG 18. `NO ACTION` would raise 23503; `RESTRICT` never does.
- **CHECK style** follows `migrations/0003_sources_up.sql`: explicit `IN` lists, and where `NULL` is meaningful a comment saying what it means *and does not mean*.
- **The repo has no `pyproject.toml`, `ruff.toml`, `pytest.ini`, `setup.cfg` or `tox.ini`** — verified. So ruff runs its defaults: `select = ["E4","E7","E9","F"]`, line length 88. **`E402` (module-level import not at top of file) is active**, and CI lints `tests/`. Every import goes in the header block.
- **The gate, in this order, before every commit:**
  ```bash
  ruff format .          # rewrites; NOT --check, or nothing ever gets fixed
  ruff check .
  pytest -q              # with DATABASE_URL exported
  ```
  `ruff format` edits in place — `git add` every file it touches, or CI fails on the committed tree while your working tree looks clean.

---

### Task 0: Preflight — prove the database is real before writing anything

**A skipped DB test is indistinguishable from a passing one in the summary line.** Every task below is worthless if the suite is silently skipping. Do this first and do not proceed until the positive control passes.

- [ ] **Step 1: Start Postgres and export the URL**

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief \
  -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
```

- [ ] **Step 2: Run the positive control**

Run: `pytest tests/test_db.py -q`
Expected: a **non-zero passed count and zero skipped**. If it says `skipped`, `DATABASE_URL` is not reaching pytest — stop and fix that. A green run with everything skipped is the failure mode this step exists to catch.

- [ ] **Step 3: Record the baseline**

Run: `pytest -q 2>&1 | tail -1`
Write the number down. Every later task compares against it. (Note: this pipe returns `tail`'s exit code, not pytest's — that is fine here because you are reading the count, not branching on the status.)

---

### Task 1: Migration scaffold and the reference tables

Creates `outlets`, `items`, `entities`, `entity_instruments`, and the test module. Adding `0006` to the real migrations directory immediately breaks `test_up_creates_the_expected_tables`, which asserts an **exact** `applied ==` list — so that is fixed here, in the same commit.

**Files:**
- Create: `migrations/0006_knowledge_base_up.sql`, `migrations/0006_knowledge_base_down.sql`, `tests/test_kb_schema.py`
- Modify: `tests/test_db.py` (`test_up_creates_the_expected_tables` only)

**Interfaces:**
- Consumes: `db.run_migrations`, `db.connect`, `db.is_configured`.
- Produces: `outlets(id, name, kind, perspective, state_funded, created_at)`; `items(id, outlet_id, url, title, body, published_at, content_hash, created_at)`; `entities(id, name, type, aliases, extractor_model, prompt_version, created_at)`; `entity_instruments(id, entity_id, symbol, market, asset_class, created_at)`. Test helpers `rejects(conn, exc)`, `_tables(conn)`, `_outlet(conn, name)`, `_entity(conn, name, type_)`. Version string `0006_knowledge_base`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_kb_schema.py`. **Every import belongs in this header block** — E402 is active and a later task adding a mid-file import will fail the gate.

```python
"""Constraint tests for the knowledge base schema (migration 0006).

Every CHECK, unique key and trigger in 0006 gets a test that tries to violate
it. A constraint nothing attempts to break is a comment: spec 12.2's argument
is that a field can look correct and carry no information, and the same is
true of a constraint nothing exercises.
"""

import contextlib

import psycopg
import pytest

import brief_memory
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing.

    Module-local, matching tests/test_db.py and tests/test_config.py. Shared in
    conftest it would be reachable from every test module by parameter name
    alone, and it runs DROP SCHEMA public CASCADE.
    """
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    """A fully migrated database, including 0006."""
    db.run_migrations(conn)
    conn.commit()
    return conn


@contextlib.contextmanager
def rejects(conn, exc):
    """Assert the block raises `exc` AND leave the transaction usable.

    db.connect sets autocommit=False (db.py:110), so a constraint violation
    aborts the whole transaction and every later statement raises
    InFailedSqlTransaction -- a sibling of the error you expected, not a
    subclass, so the test fails on the wrong line with the wrong message.
    conn.transaction() opens a SAVEPOINT when already inside a transaction and
    rolls back to it, so the next assertion in the same test still works.

    Use this for EVERY expected violation, even where a bare pytest.raises
    would happen to work today -- the next author to add a second assertion to
    the test will not know to change it.
    """
    with pytest.raises(exc):
        with conn.transaction():
            yield


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _outlet(conn, name="Reuters"):
    return conn.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _entity(conn, name="Apple", type_="company"):
    return conn.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id",
        (name, type_),
    ).fetchone()[0]


def test_outlet_kind_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, kind) VALUES ('X', 'blog')")


def test_perspective_null_is_allowed_and_a_bad_value_is_not(kb):
    """NULL means "no vantage claim made", NOT "neutral" -- calling a source
    neutral is a positive editorial claim, as contestable as picking a side."""
    kb.execute("INSERT INTO outlets (name, perspective) VALUES ('A', NULL)")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO outlets (name, perspective) VALUES ('B', 'NEUTRAL')")


def test_entity_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO entities (name, type) VALUES ('X', 'thing')")


def test_the_same_text_from_two_outlets_is_two_items(kb):
    """Syndicated wire copy must not collapse. Spec 3.4.

    A global UNIQUE (content_hash) would attribute a Reuters story carried by
    five outlets to whichever was ingested first, capping corroboration at one
    -- the opposite of why outlets was split out of sources.
    """
    for outlet_id in (_outlet(kb, "Reuters"), _outlet(kb, "Guardian")):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u', 't', 'HASH')",
            (outlet_id,),
        )
    assert kb.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_the_same_text_twice_from_one_outlet_is_rejected(kb):
    outlet_id = _outlet(kb)
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'HASH')",
        (outlet_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash) "
            "VALUES (%s, 'u2', 't2', 'HASH')",
            (outlet_id,),
        )


def test_two_instrument_mappings_with_no_market_collide(kb):
    """NULLS NOT DISTINCT, spec 3.4. Under default Postgres semantics NULLs
    compare distinct, so both rows would insert and one symbol would map to the
    same entity twice."""
    entity_id = _entity(kb, "Apple")
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (entity_id,),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
            "VALUES (%s, 'AAPL', 'equity')",
            (entity_id,),
        )


def test_asset_class_admits_index_not_only_equity_and_crypto(kb):
    """The commodity-signals-are-index-class bug: "no instrument for BRENT" was
    a wrong asset CLASS, not a missing symbol."""
    entity_id = _entity(kb, "Brent", "instrument")
    for cls in ("equity", "index", "crypto", "commodity", "fx"):
        kb.execute(
            "INSERT INTO entity_instruments (entity_id, symbol, market, asset_class) "
            "VALUES (%s, %s, 'X', %s)",
            (entity_id, f"S{cls}", cls),
        )
    assert kb.execute("SELECT count(*) FROM entity_instruments").fetchone()[0] == 5
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: all seven FAIL with `psycopg.errors.UndefinedTable: relation "outlets" does not exist`. If any passes, the database is dirty — recreate the container.

- [ ] **Step 3: Write the up migration**

Create `migrations/0006_knowledge_base_up.sql`:

```sql
-- The knowledge base: sixteen tables for the ten objects in section 3.1 of the
-- KB architecture design. Every table ships EMPTY -- nothing reads or writes
-- them until bqa.4. See docs/superpowers/specs/2026-09-02-kb-schema-ddl-design.md.
--
-- These tables carry NO user_id, unlike `sources`. 0003 states the boundary: a
-- source is part of how one person reads the world; what the sources SAY is
-- shared. Source in the KB sense is `outlets` below, NOT `sources` -- an item
-- comes from an outlet, and two readers subscribing to Reuters must not
-- produce two of it.

CREATE TABLE outlets (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    kind         TEXT        NOT NULL DEFAULT 'regional'
                 CHECK (kind IN ('wire', 'analyst', 'regional', 'primary')),
    -- NULL means "no vantage claim made", NOT "neutral": calling a source
    -- neutral is a positive editorial claim, as contestable as picking a side.
    perspective  TEXT        NULL
                 CHECK (perspective IS NULL OR perspective IN (
                     'WESTERN', 'CHINESE', 'RUSSIAN', 'IRANIAN', 'ISRAELI',
                     'ARAB', 'UKRAINIAN', 'JAPANESE', 'KOREAN', 'INDIAN')),
    state_funded BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX outlets_name ON outlets (lower(name));

CREATE TABLE items (
    id           BIGSERIAL PRIMARY KEY,
    outlet_id    BIGINT      NOT NULL REFERENCES outlets(id),
    url          TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    body         TEXT        NULL,
    published_at TIMESTAMPTZ NULL,
    content_hash TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per-outlet, NOT global. 12.3 #15 specified a content-hash dedup key for a
-- capture JSONL with no outlet dimension; promoted unchanged to a shared,
-- outlet-attributed table it would collapse syndicated wire copy and cap any
-- corroboration count at one.
CREATE UNIQUE INDEX items_outlet_hash ON items (outlet_id, content_hash);

CREATE TABLE entities (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT    NOT NULL,
    -- No default: an entity whose type is unknown is a resolution failure, not
    -- a typed row. "the Fed" is an institution, not a company.
    type            TEXT    NOT NULL
                    CHECK (type IN ('country', 'institution', 'company',
                                    'person', 'instrument')),
    aliases         TEXT[]  NOT NULL DEFAULT '{}',
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- This does NOT enforce "a company and its equity line are one entity" -- that
-- is an extraction rule for bqa.4. No unique key can express it.
CREATE UNIQUE INDEX entities_name_type ON entities (lower(name), type);

CREATE TABLE entity_instruments (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    symbol      TEXT   NOT NULL,
    market      TEXT   NULL,
    asset_class TEXT   NOT NULL
                CHECK (asset_class IN ('equity', 'index', 'crypto',
                                       'commodity', 'fx')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- symbol first: the consumer is symbol -> entity resolution, which a leading
-- entity_id cannot serve. NULLS NOT DISTINCT because market is nullable and
-- default semantics would let the same mapping insert twice.
CREATE UNIQUE INDEX entity_instruments_symbol
    ON entity_instruments (symbol, market, entity_id) NULLS NOT DISTINCT;
```

- [ ] **Step 4: Write the down migration**

Create `migrations/0006_knowledge_base_down.sql`:

```sql
-- Reverse dependency order. Later tasks PREPEND their drops, which keeps the
-- order correct automatically: each task's tables depend only on earlier ones.
DROP TABLE entity_instruments;
DROP TABLE entities;
DROP TABLE items;
DROP TABLE outlets;
```

- [ ] **Step 5: Fix the two assertions 0006 forces**

In `tests/test_db.py`, `test_up_creates_the_expected_tables` asserts an exact list *and* a table set. Both change:

```python
def test_up_creates_the_expected_tables(conn):
    applied = db.run_migrations(conn)
    assert applied == [
        "0001_runtime_foundation",
        "0002_job_runs_created_at",
        "0003_sources",
        "0004_preferences",
        "0005_runtime_state",
        "0006_knowledge_base",
    ]
    assert {
        "schema_migrations",
        "users",
        "settings",
        "job_runs",
        "sources",
        "preferences",
        "runtime_state",
        "outlets",
        "items",
        "entities",
        "entity_instruments",
    } <= _tables(conn)
```

Tasks 2–7 each extend that second set. **There is deliberately no "all sixteen tables exist" test until Task 7** — one written now would be red through six commits, and a red suite makes a real regression indistinguishable from the known failure.

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 7. `test_up_is_idempotent` now exercises `0006` for free, because it runs against the real migrations directory.

- [ ] **Step 7: Commit**

```bash
git add migrations/0006_knowledge_base_up.sql migrations/0006_knowledge_base_down.sql tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): outlets, items and entities, with item dedup per outlet"
```

---

### Task 2: Events, their entity join, and assertions

**Files:** the two migration files, `tests/test_kb_schema.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `entities`, `items` (Task 1); `rejects`, `_outlet`, `_entity`.
- Produces: `events(id, summary, occurred_at, type, commitment_state, superseded_by, extractor_model, prompt_version, created_at)`; `event_entities(event_id, entity_id, created_at)`; `assertions(id, item_id, event_id, standing, source_relationship, asserted_at, extractor_model, prompt_version, created_at)`. Helpers `_event(conn, type_, commitment)`, `_item(conn, outlet_id, content_hash)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kb_schema.py` (no new imports):

```python
def _event(conn, type_="action", commitment="in_force"):
    return conn.execute(
        "INSERT INTO events (summary, type, commitment_state) "
        "VALUES ('s', %s, %s) RETURNING id",
        (type_, commitment),
    ).fetchone()[0]


def _item(conn, outlet_id, content_hash="H"):
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', %s) RETURNING id",
        (outlet_id, content_hash),
    ).fetchone()[0]


def test_type_and_commitment_state_vary_independently(kb):
    """Spec 3.2: three orthogonal fields, because geopolitical reporting mixes
    things that happened, things people said, and things people claim
    happened, and one field cannot carry that."""
    for type_ in ("action", "statement", "disclosure"):
        for commitment in ("in_force", "committed", "intended", "proposed"):
            _event(kb, type_, commitment)
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 12


def test_event_type_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "rumour")


def test_commitment_state_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _event(kb, "action", "mooted")


def test_an_event_can_supersede_another(kb):
    """Rule 1 marks the contradicted event superseded. Presence of the FK IS
    the state -- no status enum, so a single writer cannot make it degenerate."""
    old, new = _event(kb), _event(kb)
    kb.execute("UPDATE events SET superseded_by = %s WHERE id = %s", (new, old))
    assert (
        kb.execute("SELECT superseded_by FROM events WHERE id = %s", (old,)).fetchone()[0]
        == new
    )


def test_standing_rejects_an_unknown_value(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'rumoured')",
            (item_id, event_id),
        )


def test_source_relationship_is_nullable_with_no_default(kb):
    """Spec 2.1: the one extracted enum with no anchor gets NO default -- a
    NOT NULL DEFAULT is the fastest route to the degenerate outcome 12.2 rates
    worse than a missing field. Absent means "not labelled", never
    "independent"."""
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    assert kb.execute("SELECT source_relationship FROM assertions").fetchone()[0] is None


def test_one_item_asserts_one_event_once(kb):
    item_id, event_id = _item(kb, _outlet(kb)), _event(kb)
    kb.execute(
        "INSERT INTO assertions (item_id, event_id, standing) "
        "VALUES (%s, %s, 'reported')",
        (item_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO assertions (item_id, event_id, standing) "
            "VALUES (%s, %s, 'official')",
            (item_id, event_id),
        )
```

- [ ] **Step 2: Run to verify they fail**

Run:
```bash
pytest tests/test_kb_schema.py -q \
  --deselect tests/test_kb_schema.py::test_outlet_kind_rejects_an_unknown_value
```
Simpler and more reliable: run the whole file, `pytest tests/test_kb_schema.py -q`, and confirm the **seven new tests** fail with `relation "events" does not exist` while the seven from Task 1 still pass. Do not use `-k` substring selectors — they over-match as the file grows.

- [ ] **Step 3: Append the tables to the up migration**

```sql
CREATE TABLE events (
    id               BIGSERIAL PRIMARY KEY,
    summary          TEXT        NOT NULL,
    occurred_at      TIMESTAMPTZ NULL,
    -- Three orthogonal fields, spec 3.2. "Trump declared the ceasefire over"
    -- is statement/intended/official; whether the ceasefire IS over is a
    -- separate Claim with its own standing. Conflating them is what the Day 1
    -- brief did.
    type             TEXT        NOT NULL
                     CHECK (type IN ('action', 'statement', 'disclosure')),
    commitment_state TEXT        NOT NULL
                     CHECK (commitment_state IN ('in_force', 'committed',
                                                 'intended', 'proposed')),
    -- Presence IS the superseded state; rule 1 writes it.
    superseded_by    BIGINT      NULL REFERENCES events(id),
    extractor_model  TEXT        NULL,
    prompt_version   INTEGER     NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- NULLS LAST: occurred_at is nullable and DESC defaults to NULLS FIRST, which
-- would put every undated event at the head of rule 1's most-recent scan.
CREATE INDEX events_occurred ON events (occurred_at DESC NULLS LAST);

CREATE TABLE event_entities (
    event_id   BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id  BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX event_entities_entity ON event_entities (entity_id);

CREATE TABLE assertions (
    id                  BIGSERIAL PRIMARY KEY,
    item_id             BIGINT      NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event_id            BIGINT      NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    -- Per assertion, not per event: two sources can assert the same event with
    -- very different weight.
    standing            TEXT        NOT NULL
                        CHECK (standing IN ('verified', 'official', 'reported',
                                            'attributed', 'alleged')),
    -- Nullable, NO default. The only extracted enum with no worked example
    -- anywhere in the parent spec -- severity's exact provenance. NULL means
    -- "not labelled", NOT "independent". news-brief-bqa.8 measures it.
    source_relationship TEXT        NULL
                        CHECK (source_relationship IS NULL OR source_relationship IN
                               ('party', 'aligned', 'independent', 'adversarial')),
    asserted_at         TIMESTAMPTZ NULL,
    extractor_model     TEXT        NULL,
    prompt_version      INTEGER     NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX assertions_item_event ON assertions (item_id, event_id);
```

- [ ] **Step 4: Prepend to the down migration**

```sql
DROP TABLE assertions;
DROP TABLE event_entities;
DROP TABLE events;
```

- [ ] **Step 5: Extend the table set in `tests/test_db.py`**

Add `"events"`, `"event_entities"`, `"assertions"`.

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 14.

- [ ] **Step 7: Commit**

```bash
git add migrations/ tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): events carry three orthogonal fields, and standing rides the assertion"
```

---

### Task 3: Observations

**Files:** the two migration files, `tests/test_kb_schema.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `entities` (Task 1).
- Produces: `observations(id, entity_id, symbol, metric, value, return_window, observed_at, provider, created_at)`. Helper `_observation(conn, entity_id, metric, value, window)`.

- [ ] **Step 1: Write the failing tests**

```python
def _observation(conn, entity_id, metric="price", value=100, window=None):
    return conn.execute(
        "INSERT INTO observations (entity_id, symbol, metric, value, return_window, "
        "observed_at, provider) VALUES (%s, 'S', %s, %s, %s, now(), 'yahoo') RETURNING id",
        (entity_id, metric, value, window),
    ).fetchone()[0]


def test_a_return_without_a_window_is_rejected(kb):
    """Spec 2.2: a return without a period is not a number."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "return", 0.13, None)


def test_a_price_with_a_window_is_rejected(kb):
    """The biconditional runs both ways: a level has no window."""
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "price", 100, "1d")


def test_a_return_and_a_price_are_two_separate_rows(kb):
    """The replay's "SK Hynix +13%" is a return row; the level it moved from is
    a separate price row. Conflating them is how a one-day bounce became
    confirmation of a multi-quarter thesis."""
    entity_id = _entity(kb, "SK Hynix")
    _observation(kb, entity_id, "return", 0.13, "1d")
    _observation(kb, entity_id, "price", 100)
    assert kb.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_metric_rejects_an_unknown_value(kb):
    entity_id = _entity(kb, "SK Hynix")
    with rejects(kb, psycopg.errors.CheckViolation):
        _observation(kb, entity_id, "sentiment")


def test_observations_carry_provider_not_extractor_model(kb):
    """Spec 3.5: observations are FETCHED, not extracted. A column named
    extractor_model holding 'yahoo' would be a lie in the one field that exists
    to make silent model drift detectable."""
    cols = {
        r[0]
        for r in kb.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'observations'"
        ).fetchall()
    }
    assert "provider" in cols
    assert "extractor_model" not in cols
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: the five new tests FAIL with `relation "observations" does not exist`; the fourteen earlier ones still pass.

- [ ] **Step 3: Append to the up migration**

```sql
CREATE TABLE observations (
    id            BIGSERIAL PRIMARY KEY,
    entity_id     BIGINT      NOT NULL REFERENCES entities(id),
    -- The symbol ACTUALLY queried at the provider, stored beside the result it
    -- produced. Deliberate duplication with entity_instruments: the AVAV_
    -- double-underscore bug was invisible precisely because the queried symbol
    -- was never recorded next to its result.
    symbol        TEXT        NOT NULL,
    -- No default: an observation without a metric is unusable, and a default
    -- would silently mislabel rows rather than reject them.
    metric        TEXT        NOT NULL
                  CHECK (metric IN ('price', 'return', 'yield', 'volume', 'spread')),
    value         NUMERIC     NOT NULL,
    return_window TEXT        NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    -- NOT extractor_model: these rows are fetched, not extracted. Spec 3.5.
    provider      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Biconditional, both directions. metric is NOT NULL so neither side can
    -- be NULL and there is no three-valued-logic hole.
    CHECK ((metric = 'return') = (return_window IS NOT NULL))
);
CREATE INDEX observations_entity_observed ON observations (entity_id, observed_at DESC);
```

- [ ] **Step 4: Prepend to the down migration**

```sql
DROP TABLE observations;
```

- [ ] **Step 5: Add `"observations"` to the table set in `tests/test_db.py`**

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 19.

- [ ] **Step 7: Commit**

```bash
git add migrations/ tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): observations, where a return must state its window"
```

---

### Task 4: Claims

The largest table and the one with a live incumbent. Its columns are a strict superset of the eighteen keys `brief_memory.merge_ledger` writes (spec §5.1).

**Files:** the two migration files, `tests/test_kb_schema.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `events` (Task 2).
- Produces: `claims(id, ledger_id, claim, topic, status, origin, severity, horizon_days, resolution_date, horizon_elapsed, falsifier, falsifier_kind, first_seen, last_reaffirmed, restate_count, source_count, driver, resolved_on, broken_by_note, broken_by_event_id, extractor_model, prompt_version, created_at)`. Helper `_claim(conn, text, status, resolved_on, **kw)`.

- [ ] **Step 1: Write the failing tests**

```python
def _claim(conn, text="a claim", status="standing", resolved_on=None, **kw):
    cols = ["claim", "status", "first_seen", "resolved_on"]
    vals = [text, status, "2026-09-01", resolved_on]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO claims ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        vals,
    ).fetchone()[0]


def test_status_admits_all_six_states(kb):
    """Spec 2.1: one lifecycle, one column. resolved_outcome does not exist --
    two enums with 'broken' in both permitted status='standing' alongside
    resolved_outcome='broken', and left rule 4's 'stale' homeless."""
    for status in ("standing", "challenged", "broken", "confirmed", "expired", "withdrawn"):
        resolved = None if status == "standing" else "2026-09-02"
        extra = {"broken_by_note": "n"} if status == "broken" else {}
        _claim(kb, f"c {status}", status, resolved, **extra)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 6


def test_status_rejects_stale_which_is_spelled_expired_here(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "stale", "2026-09-02")


def test_a_non_standing_claim_must_record_when_it_left_standing(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "expired", None)


def test_horizon_elapsed_requires_a_resolution(kb):
    """Elapsed days mean nothing without the resolution they are measured to."""
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "standing", None, horizon_elapsed=2)


def test_a_broken_claim_must_say_what_broke_it(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _claim(kb, "c", "broken", "2026-09-02")


def test_a_broken_claim_accepts_either_a_note_or_an_event(kb):
    """The incumbent writes free text ('unmarked rewrite: ...'); rule 1 wants
    the contradicting event. A single BIGINT FK could not hold the existing
    values, which is why there are two columns."""
    event_id = _event(kb)
    _claim(kb, "c1", "broken", "2026-09-02", broken_by_note="unmarked rewrite: x")
    _claim(kb, "c2", "broken", "2026-09-02", broken_by_event_id=event_id)
    assert kb.execute("SELECT count(*) FROM claims").fetchone()[0] == 2


def test_horizon_days_rejects_zero_negatives_and_over_ten_years(kb):
    """Matches the 1..3650 range _coerce_horizon_days already enforces
    (_MAX_HORIZON_DAYS). Without it, resolution_date lands on or before
    first_seen and rule 4 fires the moment the claim is created.

    Three assertions in one test: this is exactly why `rejects` uses a
    savepoint. A bare pytest.raises would leave the transaction aborted and the
    second iteration would raise InFailedSqlTransaction instead.
    """
    for bad in (0, -1, 3651):
        with rejects(kb, psycopg.errors.CheckViolation):
            _claim(kb, f"c{bad}", horizon_days=bad)


def test_horizon_days_null_is_allowed_and_means_unknown(kb):
    """Spec 2.2: absence is not a short horizon. Never defaulted -- a default
    would manufacture calibration data."""
    claim_id = _claim(kb, "c", horizon_days=None)
    assert (
        kb.execute("SELECT horizon_days FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        is None
    )


def test_claims_without_a_ledger_ancestor_coexist(kb):
    """ledger_id is UNIQUE but nullable, and NULLs are DISTINCT by default --
    deliberately, so KB-native claims need no synthetic id."""
    _claim(kb, "c1")
    _claim(kb, "c2")
    assert kb.execute("SELECT count(*) FROM claims WHERE ledger_id IS NULL").fetchone()[0] == 2


def test_a_ledger_id_cannot_be_reused(kb):
    """merge_ledger treats an echoed id as authoritative, so two rows claiming
    c-0001 would make the model's citation ambiguous."""
    _claim(kb, "c1", ledger_id="c-0001")
    with rejects(kb, psycopg.errors.UniqueViolation):
        _claim(kb, "c2", ledger_id="c-0001")
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: the ten new tests FAIL with `relation "claims" does not exist`.

- [ ] **Step 3: Append to the up migration**

```sql
CREATE TABLE claims (
    id                 BIGSERIAL PRIMARY KEY,
    -- Preserves the JSON ledger's string identity, format 'c-0001' per
    -- merge_ledger and the r"c-(\d+)$" regex in _max_id_num. merge_ledger
    -- treats an echoed id as authoritative, so a BIGSERIAL alone would
    -- silently renumber every claim the model can cite.
    ledger_id          TEXT    NULL,
    -- Named `claim`, not `text`: that is the ledger's key. Spec 5.1.
    claim              TEXT    NOT NULL,
    topic              TEXT    NULL,
    -- ONE lifecycle in ONE column.
    --
    -- WARNING for bqa.4: brief_memory._VALID_STATUS knows only the first
    -- three. Until it is widened, confirmed/expired/withdrawn coerce back to
    -- 'standing', the TTL deletes them after 7 days, and select_working_set
    -- renders them as live fact. See news-brief-bqa.9 and spec section 6.
    status             TEXT    NOT NULL DEFAULT 'standing'
                       CHECK (status IN ('standing', 'challenged', 'broken',
                                         'confirmed', 'expired', 'withdrawn')),
    origin             TEXT    NOT NULL DEFAULT 'extracted'
                       CHECK (origin IN ('extracted', 'authored')),
    -- Measured DEGENERATE (high 25/25) and shipping anyway, because it is
    -- load-bearing: _ttl_bonus grants 'high' extra retention days and
    -- _severity_rank orders the working-set prefix. Owes a rubric before any
    -- NEW consumer reads it (news-brief-bqa.8).
    severity           TEXT    NOT NULL DEFAULT 'normal'
                       CHECK (severity IN ('low', 'normal', 'high')),
    -- 1..3650 matches _MAX_HORIZON_DAYS. NULL means the horizon could not be
    -- determined and the claim is EXEMPT from rule 4 -- never defaulted.
    horizon_days       INTEGER NULL CHECK (horizon_days IS NULL
                                           OR horizon_days BETWEEN 1 AND 3650),
    resolution_date    DATE    NULL,
    horizon_elapsed    INTEGER NULL,
    falsifier          TEXT    NULL,
    falsifier_kind     TEXT    NULL
                       CHECK (falsifier_kind IS NULL OR falsifier_kind IN
                              ('event_triggered', 'review_required')),
    first_seen         DATE    NOT NULL,
    last_reaffirmed    DATE    NULL,
    restate_count      INTEGER NOT NULL DEFAULT 0,
    source_count       INTEGER NULL,
    driver             TEXT    NULL,
    -- The date the claim LEFT 'standing', in either direction. Named
    -- resolved_on rather than the ledger's broke_on because three of the six
    -- statuses are not breaks; spec 5.1 maps the key across.
    resolved_on        DATE    NULL,
    -- RESTRICT, not CASCADE: deleting the contradicting event must never
    -- silently un-break a claim.
    broken_by_note     TEXT    NULL,
    broken_by_event_id BIGINT  NULL REFERENCES events(id) ON DELETE RESTRICT,
    extractor_model    TEXT    NULL,
    prompt_version     INTEGER NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status = 'standing' OR resolved_on IS NOT NULL),
    CHECK (horizon_elapsed IS NULL OR resolved_on IS NOT NULL),
    CHECK (status <> 'broken'
           OR num_nonnulls(broken_by_note, broken_by_event_id) >= 1)
);
CREATE UNIQUE INDEX claims_ledger_id ON claims (ledger_id);
-- Rule 4's predicate exactly: a claim that expires LEAVES this index. An
-- earlier draft indexed WHERE status = 'standing' while rule 4 wrote a
-- different column, so an expired claim never left and rule 4 re-fired on it
-- every morning for the life of the row.
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged');
```

- [ ] **Step 4: Prepend to the down migration**

```sql
DROP TABLE claims;
```

- [ ] **Step 5: Add `"claims"` to the table set in `tests/test_db.py`**

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 29.

- [ ] **Step 7: Commit**

```bash
git add migrations/ tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): claims, with one lifecycle column and a superset of the ledger row"
```

---

### Task 5: Claim evidence and the evidence floor

Rule 3's whole job is refusing to double-count. A table permitting duplicate evidence defeats it silently.

**Files:** the two migration files, `tests/test_kb_schema.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `claims` (Task 4), `events` (Task 2), `observations` (Task 3).
- Produces: `claim_evidence(id, claim_id, event_id, observation_id, span_start, span_end, created_at)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_evidence_pointing_at_neither_is_rejected(kb):
    claim_id = _claim(kb, "c")
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute("INSERT INTO claim_evidence (claim_id) VALUES (%s)", (claim_id,))


def test_evidence_pointing_at_both_is_rejected(kb):
    """The both-set case is the one that silently corrupts rule 3's count."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    observation_id = _observation(kb, _entity(kb, "SK Hynix"))
    with rejects(kb, psycopg.errors.CheckViolation):
        kb.execute(
            "INSERT INTO claim_evidence (claim_id, event_id, observation_id) "
            "VALUES (%s, %s, %s)",
            (claim_id, event_id, observation_id),
        )


def test_the_same_evidence_cannot_be_counted_twice(kb):
    """NULLS NOT DISTINCT. Under default semantics both rows insert, one piece
    of evidence counts as two, and the evidence floor is cleared by a
    duplicate -- the chip-whipsaw failure rule 3 exists to prevent."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    kb.execute(
        "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
        (claim_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
            (claim_id, event_id),
        )


def test_deleting_an_event_cannot_silently_lower_the_evidence_floor(kb):
    """RESTRICT, not CASCADE: a floor an unrelated delete can lower is not a
    floor."""
    claim_id, event_id = _claim(kb, "c"), _event(kb)
    kb.execute(
        "INSERT INTO claim_evidence (claim_id, event_id) VALUES (%s, %s)",
        (claim_id, event_id),
    )
    with rejects(kb, psycopg.errors.RestrictViolation):
        kb.execute("DELETE FROM events WHERE id = %s", (event_id,))
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: the four new tests FAIL with `relation "claim_evidence" does not exist`.

- [ ] **Step 3: Append to the up migration**

```sql
CREATE TABLE claim_evidence (
    id             BIGSERIAL PRIMARY KEY,
    claim_id       BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- RESTRICT, not CASCADE: rule 3 is a COUNT over these rows, and a floor an
    -- unrelated delete can silently lower is not a floor.
    event_id       BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    observation_id BIGINT NULL REFERENCES observations(id) ON DELETE RESTRICT,
    span_start     DATE   NULL,
    span_end       DATE   NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, observation_id) = 1)
);
-- NULLS NOT DISTINCT: without it, (claim, event, NULL) inserts twice and one
-- piece of evidence counts as two. Also serves the claim_id prefix lookup, so
-- no separate FK index is created.
CREATE UNIQUE INDEX claim_evidence_unique
    ON claim_evidence (claim_id, event_id, observation_id) NULLS NOT DISTINCT;
```

- [ ] **Step 4: Prepend to the down migration**

```sql
DROP TABLE claim_evidence;
```

- [ ] **Step 5: Add `"claim_evidence"` to the table set in `tests/test_db.py`**

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 33.

- [ ] **Step 7: Commit**

```bash
git add migrations/ tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): claim evidence, where the same row cannot be counted twice"
```

---

### Task 6: The claim-text immutability trigger

Defence in depth for the 2026-08-29 Patriot failure. The primary enforcement stays `brief_memory._reaffirm`, which catches a case no constraint can see.

**Files:** the two migration files, `tests/test_kb_schema.py`

**Interfaces:**
- Consumes: `claims` (Task 4).
- Produces: function `claims_freeze_claim_text()`, trigger `claims_freeze_claim_text_trg`.

- [ ] **Step 1: Write the failing tests — four cases, and the third is the point**

```python
def test_a_standing_claim_may_still_be_refined(kb):
    """Negative control: rewording stays correct for a claim that is, and
    remains, standing. Expected to pass BEFORE the trigger exists too."""
    claim_id = _claim(kb, "original")
    kb.execute("UPDATE claims SET claim = 'refined' WHERE id = %s", (claim_id,))
    assert (
        kb.execute("SELECT claim FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        == "refined"
    )


def test_an_already_broken_claim_cannot_be_reworded(kb):
    claim_id = _claim(kb, "original", "broken", "2026-09-02", broken_by_note="n")
    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute("UPDATE claims SET claim = 'rewritten' WHERE id = %s", (claim_id,))


def test_breaking_and_rewriting_in_one_statement_is_rejected(kb):
    """THE Patriot mechanism. One reply marked the claim broken AND rewrote it
    into a description of its own reversal, so the ledger read back as though
    the reversal had itself been reversed.

    A predicate reading OLD.status alone lets this through, because OLD.status
    is still 'standing' at that moment. This test is why the trigger reads both
    tuples, and it is the one a test written to the constraint rather than to
    the failure would miss.
    """
    claim_id = _claim(kb, "Trump agreed to license Patriot production")
    with rejects(kb, psycopg.errors.RaiseException):
        kb.execute(
            "UPDATE claims SET status = 'broken', resolved_on = '2026-09-02', "
            "broken_by_note = 'reversed', claim = 'Trump reversed course' "
            "WHERE id = %s",
            (claim_id,),
        )


def test_the_status_may_change_without_touching_the_text(kb):
    """Breaking a claim is normal; only rewriting it is forbidden. The reversal
    belongs in broken_by, not in the claim text."""
    claim_id = _claim(kb, "original")
    kb.execute(
        "UPDATE claims SET status = 'broken', resolved_on = '2026-09-02', "
        "broken_by_note = 'n' WHERE id = %s",
        (claim_id,),
    )
    assert (
        kb.execute("SELECT claim FROM claims WHERE id = %s", (claim_id,)).fetchone()[0]
        == "original"
    )
```

- [ ] **Step 2: Run to verify the right ones fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: `test_an_already_broken_claim_cannot_be_reworded` and `test_breaking_and_rewriting_in_one_statement_is_rejected` FAIL (the `UPDATE`s succeed — no trigger yet). The other two PASS. If all four pass, the trigger already exists and something is out of order.

- [ ] **Step 3: Append the function and trigger to the up migration**

```sql
-- Defence in depth. The PRIMARY enforcement is brief_memory._reaffirm, which
-- also catches the case no constraint can see: an unmarked rewrite that keeps
-- status 'standing' while editing the text to match new facts. Three gold-set
-- runs measured the model doing exactly that on every true break it scored
-- standing.
--
-- The predicate reads BOTH tuples. Reading OLD.status alone would permit the
-- single UPDATE that marks a claim broken AND rewrites it -- precisely the
-- 2026-08-29 Patriot mechanism this exists to stop.
CREATE OR REPLACE FUNCTION claims_freeze_claim_text() RETURNS trigger AS $$
BEGIN
    IF (OLD.status <> 'standing' OR NEW.status <> 'standing')
       AND NEW.claim IS DISTINCT FROM OLD.claim THEN
        RAISE EXCEPTION 'claim text is immutable once status leaves standing (id %)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claims_freeze_claim_text_trg
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_freeze_claim_text();
```

`RAISE EXCEPTION` with no `USING ERRCODE` raises SQLSTATE `P0001`, which psycopg3 surfaces as `psycopg.errors.RaiseException`. The un-doubled `%` is safe: `db.run_migrations` calls `conn.execute(sql)` with no parameters, so psycopg skips placeholder conversion entirely and sends the file over the simple query protocol.

- [ ] **Step 4: Add the function drop to the END of the down migration**

`DROP TABLE claims` removes the trigger but **not** the function. This line goes after every `DROP TABLE`, and it is the only line in the down file that is not a table drop:

```sql
DROP FUNCTION claims_freeze_claim_text();
```

- [ ] **Step 5: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 37.

- [ ] **Step 6: Commit**

```bash
git add migrations/ tests/test_kb_schema.py
git commit -m "feat(kb): a broken claim cannot be rewritten into its own reversal"
```

---

### Task 7: Theses, stories and links, and the whole-schema assertion

The remaining six tables. All provisional per spec §1.2 — empty, and reshapeable without ceremony until `bqa.4` writes them. This is also where the "all sixteen exist" assertion lands, because this is the first point at which it can be green.

**Files:** the two migration files, `tests/test_kb_schema.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `claims`, `events`, `observations`, `stories`.
- Produces: `theses`, `thesis_claims`, `stories`, `story_members`, `open_questions`, `links`; module constant `KB_TABLES`.

- [ ] **Step 1: Write the failing tests**

```python
KB_TABLES = {
    "outlets",
    "items",
    "entities",
    "entity_instruments",
    "events",
    "event_entities",
    "assertions",
    "observations",
    "claims",
    "claim_evidence",
    "theses",
    "thesis_claims",
    "stories",
    "story_members",
    "open_questions",
    "links",
}


def _story(conn, name="Hormuz", scope="structural"):
    return conn.execute(
        "INSERT INTO stories (name, scope) VALUES (%s, %s) RETURNING id", (name, scope)
    ).fetchone()[0]


def _thesis(conn, text="t"):
    return conn.execute(
        "INSERT INTO theses (text) VALUES (%s) RETURNING id", (text,)
    ).fetchone()[0]


def test_0006_creates_all_sixteen_kb_tables(kb):
    assert KB_TABLES <= _tables(kb)


def test_confidence_defaults_to_speculative(kb):
    """Spec 2.3: speculative means NO supporting claims at all. An earlier
    draft said "none resolved", which overlapped tentative completely and left
    the ladder undetermined at the value every thesis starts on."""
    _thesis(kb)
    assert kb.execute("SELECT confidence FROM theses").fetchone()[0] == "speculative"


def test_a_claim_supports_or_undermines_a_thesis_but_not_both(kb):
    thesis_id, claim_id = _thesis(kb), _claim(kb, "c")
    kb.execute(
        "INSERT INTO thesis_claims (thesis_id, claim_id, role) "
        "VALUES (%s, %s, 'supporting')",
        (thesis_id, claim_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO thesis_claims (thesis_id, claim_id, role) "
            "VALUES (%s, %s, 'undermining')",
            (thesis_id, claim_id),
        )


def test_a_new_story_is_active_with_no_material_change(kb):
    """Spec 2.3: the state must be defined at the default. NULL
    last_material_change means "created, no members yet" and reads active --
    the same hole that made the old confidence ladder undetermined."""
    _story(kb)
    assert kb.execute("SELECT state, last_material_change FROM stories").fetchone() == (
        "active",
        None,
    )


def test_story_scope_rejects_an_unknown_value(kb):
    with rejects(kb, psycopg.errors.CheckViolation):
        _story(kb, "X", "ongoing")


def test_a_story_cannot_hold_the_same_event_twice(kb):
    """3.5 forbids rebuilding the member list, so a duplicate row renders as a
    duplicate line in the brief with no read-time dedup to catch it."""
    story_id, event_id = _story(kb, "S", "episodic"), _event(kb)
    kb.execute(
        "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
        (story_id, event_id),
    )
    with rejects(kb, psycopg.errors.UniqueViolation):
        kb.execute(
            "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
            (story_id, event_id),
        )


def test_deleting_an_event_cannot_retcon_a_member_list(kb):
    """A cascade here is a silent retcon of the stored list -- the Day 3
    failure mechanism."""
    story_id, event_id = _story(kb, "S", "episodic"), _event(kb)
    kb.execute(
        "INSERT INTO story_members (story_id, event_id) VALUES (%s, %s)",
        (story_id, event_id),
    )
    with rejects(kb, psycopg.errors.RestrictViolation):
        kb.execute("DELETE FROM events WHERE id = %s", (event_id,))


def test_a_link_must_carry_a_decay_check_date(kb):
    """NOT NULL, derived from expected_persistence at write time. A nullable
    decay date leaves a link 'unchecked' forever, never entering rule 5 -- and
    2.3's negative case makes that permanent rather than eventually-noticed."""
    observation_id = _observation(kb, _entity(kb, "Brent", "instrument"))
    with rejects(kb, psycopg.errors.NotNullViolation):
        kb.execute(
            "INSERT INTO links (event_id, observation_id, mechanism, effect_kind, "
            "expected_persistence) VALUES (%s, %s, 'm', 'flow', 'session')",
            (_event(kb), observation_id),
        )


def test_effect_kind_admits_all_four_decay_behaviours(kb):
    """Confusing a re_rating driver with flow is what produced three
    contradictory chip verdicts in 72 hours."""
    observation_id = _observation(kb, _entity(kb, "Brent", "instrument"))
    for kind in ("re_rating", "risk_premium", "flow", "fundamental_revision"):
        kb.execute(
            "INSERT INTO links (event_id, observation_id, mechanism, effect_kind, "
            "expected_persistence, decay_check_date) "
            "VALUES (%s, %s, 'm', %s, 'days', '2026-09-10')",
            (_event(kb), observation_id, kind),
        )
    assert kb.execute("SELECT count(*) FROM links").fetchone()[0] == 4


def test_an_open_question_defaults_to_open(kb):
    story_id = _story(kb, "S", "episodic")
    kb.execute("INSERT INTO open_questions (story_id, text) VALUES (%s, 'q')", (story_id,))
    assert kb.execute("SELECT status FROM open_questions").fetchone()[0] == "open"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_kb_schema.py -q`
Expected: the ten new tests FAIL — `test_0006_creates_all_sixteen_kb_tables` on the assertion, the rest with `relation "theses" does not exist`.

- [ ] **Step 3: Append the six tables to the up migration**

```sql
-- The six tables below are PROVISIONAL (spec 1.2): empty, read by nothing, and
-- reshapeable without ceremony until bqa.4 writes them. theses and
-- thesis_claims have no consumer at all before Epic 6 -- they are here because
-- the schema was scoped to all ten objects, not because anything needs them.

CREATE TABLE theses (
    id              BIGSERIAL PRIMARY KEY,
    text            TEXT    NOT NULL,
    -- Ordinal, NO numeric scoring: Bayesian-looking arithmetic over ordinal
    -- judgments manufactures precision the inputs do not contain. Advances
    -- ONLY on RESOLVED supporting claims, never on their count.
    confidence      TEXT    NOT NULL DEFAULT 'speculative'
                    CHECK (confidence IN ('speculative', 'tentative',
                                          'supported', 'established')),
    horizon_days    INTEGER NULL CHECK (horizon_days IS NULL
                                        OR horizon_days BETWEEN 1 AND 3650),
    triggers        TEXT[]  NOT NULL DEFAULT '{}',
    -- Provenance but NOT origin: provenance says which model wrote the row,
    -- which drift detection needs. origin says whether a rule may read it as
    -- evidence, and for a thesis the answer is always no -- so the column
    -- would be uniform, which 12.2 rates worse than missing.
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE thesis_claims (
    thesis_id  BIGINT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
    claim_id   BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- One table, not two: supporting and undermining are the same edge with
    -- opposite sign, and the PK stops a claim being both.
    role       TEXT   NOT NULL CHECK (role IN ('supporting', 'undermining')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thesis_id, claim_id)
);

CREATE TABLE stories (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL,
    -- Structural stories outlive their parent topics: arms sovereignty
    -- survives the war's end. No hierarchy -- reality does not fit a tree.
    scope                TEXT NOT NULL CHECK (scope IN ('episodic', 'structural')),
    -- NULL last_material_change means "created, no members yet" and reads
    -- 'active'; the state must be defined at the default. Silence is
    -- 'dormant', never 'closed' -- only a review action closes a story.
    state                TEXT NOT NULL DEFAULT 'active'
                         CHECK (state IN ('active', 'dormant', 'closed')),
    last_material_change TIMESTAMPTZ NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX stories_name ON stories (lower(name));

CREATE TABLE story_members (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    -- RESTRICT: 3.5 forbids rebuilding the member list, so a cascade would be
    -- a silent retcon of it -- the Day 3 failure mechanism.
    -- Member STATUS is deliberately NOT a column: it is derivable by join
    -- (superseded_by set, or the claim broken/expired/withdrawn), and a stored
    -- copy could go stale.
    event_id   BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    claim_id   BIGINT NULL REFERENCES claims(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, claim_id) = 1)
);
CREATE UNIQUE INDEX story_members_unique
    ON story_members (story_id, event_id, claim_id) NULLS NOT DISTINCT;

CREATE TABLE open_questions (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text       TEXT   NOT NULL,
    due_date   DATE   NULL,
    status     TEXT   NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'answered', 'dropped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX open_questions_due ON open_questions (due_date) WHERE status = 'open';

CREATE TABLE links (
    id                   BIGSERIAL PRIMARY KEY,
    -- event -> observation only. Event -> event links are out of scope:
    -- nothing in 3.4 or the five rules needs them, and rule 2 ("an Observation
    -- with no explaining Link") is a clean LEFT JOIN under this shape.
    event_id             BIGINT  NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    observation_id       BIGINT  NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    mechanism            TEXT    NOT NULL,
    -- How long the event keeps explaining the move. Confusing a re_rating
    -- driver with flow produced three contradictory chip verdicts in 72 hours.
    effect_kind          TEXT    NOT NULL
                         CHECK (effect_kind IN ('re_rating', 'risk_premium',
                                                'flow', 'fundamental_revision')),
    expected_persistence TEXT    NOT NULL
                         CHECK (expected_persistence IN ('session', 'days',
                                                         'weeks', 'structural')),
    -- NOT NULL, derived at write time from expected_persistence. A nullable
    -- decay date leaves a link 'unchecked' forever and never in rule 5.
    decay_check_date     DATE    NOT NULL,
    falsifier            TEXT    NULL,
    -- 'unchecked' does NOT become 'decayed' through the passage of time, or
    -- the persistence priors learn from measurements that never happened.
    status               TEXT    NOT NULL DEFAULT 'unchecked'
                         CHECK (status IN ('unchecked', 'active', 'decayed', 'refuted')),
    origin               TEXT    NOT NULL DEFAULT 'extracted'
                         CHECK (origin IN ('extracted', 'authored')),
    extractor_model      TEXT    NULL,
    prompt_version       INTEGER NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX links_observation ON links (observation_id);
CREATE INDEX links_decay_due ON links (decay_check_date) WHERE status = 'unchecked';
```

- [ ] **Step 4: Prepend to the down migration**

These go above the existing drops and below nothing — the `DROP FUNCTION` line stays last:

```sql
DROP TABLE links;
DROP TABLE open_questions;
DROP TABLE story_members;
DROP TABLE stories;
DROP TABLE thesis_claims;
DROP TABLE theses;
```

- [ ] **Step 5: Complete the table set in `tests/test_db.py`**

Add `"theses"`, `"thesis_claims"`, `"stories"`, `"story_members"`, `"open_questions"`, `"links"`.

- [ ] **Step 6: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 47.

- [ ] **Step 7: Commit**

```bash
git add migrations/ tests/test_kb_schema.py tests/test_db.py
git commit -m "feat(kb): theses, stories and links, all provisional until something writes them"
```

---

### Task 8: Prove the rollback actually runs

**No down migration in this repo has ever been executed by a test.** The three existing down tests run under `test_db.py`'s `two_migrations` fixture, which monkeypatches `db.MIGRATIONS_DIR` to a temp directory holding only `0001` plus a throwaway `0002` — deliberately, per its docstring, because with one migration a correct `down` and one that drops the database are indistinguishable. The consequence is that `0006`'s down, which drops sixteen tables and a function, is untested. It is also the largest rollback in the repo, and spec §8 names this test as the only thing standing between the migration and an un-runnable rollback discovered during an incident.

**Files:** `tests/test_kb_schema.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7. Produces: no schema change. Helper `_functions(conn)`.

- [ ] **Step 1: Write the failing test, and its automated negative control**

```python
def _functions(conn) -> set[str]:
    rows = conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _copy_migrations(tmp_path):
    for p in db.MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / p.name).write_text(
            p.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return tmp_path


def test_0006_rolls_back_and_reapplies_against_the_real_directory(conn):
    """The only test in this repo that executes a real down migration.

    Three things can go wrong and only this catches them: a wrong FK drop
    order, a function left behind by DROP TABLE, and a CREATE FUNCTION that
    should have been CREATE OR REPLACE. The last one shows only on the second
    up.
    """
    db.run_migrations(conn)
    conn.commit()
    assert KB_TABLES <= _tables(conn)
    assert "claims_freeze_claim_text" in _functions(conn)

    reverted = db.run_migrations(conn, direction="down")
    conn.commit()

    assert reverted == ["0006_knowledge_base"]
    assert not (KB_TABLES & _tables(conn)), "a KB table survived the rollback"
    assert "claims_freeze_claim_text" not in _functions(conn), (
        "DROP TABLE does not drop a function; the down migration must drop it"
    )
    assert {"users", "settings", "job_runs", "sources"} <= _tables(conn), (
        "rolling back one step must not take the prior migrations with it"
    )

    reapplied = db.run_migrations(conn)
    conn.commit()
    assert reapplied == ["0006_knowledge_base"]
    assert KB_TABLES <= _tables(conn)


def test_the_rollback_assertion_can_actually_fail(conn, tmp_path, monkeypatch):
    """Negative control for the test above, automated rather than manual.

    A rollback assertion that has never failed proves nothing. This copies the
    real migrations to a temp directory, strips DROP FUNCTION from 0006's down
    file, and asserts the function then SURVIVES the rollback -- which is what
    makes the assertion above meaningful.

    Done as a test rather than as a comment-out-and-restore ritual, because a
    manual negative control is the step that gets skipped under time pressure,
    and it leaves a window where an interrupt commits the migration without its
    function drop.
    """
    tmp = _copy_migrations(tmp_path)
    down = tmp / "0006_knowledge_base_down.sql"
    down.write_text(
        "\n".join(
            line
            for line in down.read_text(encoding="utf-8").splitlines()
            if "DROP FUNCTION" not in line
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp)

    db.run_migrations(conn)
    conn.commit()
    db.run_migrations(conn, direction="down")
    conn.commit()

    assert "claims_freeze_claim_text" in _functions(conn), (
        "stripping DROP FUNCTION should leave the function behind; if it does "
        "not, the assertion in the test above cannot fail and proves nothing"
    )
```

- [ ] **Step 2: Run both**

Run: `pytest tests/test_kb_schema.py -q`
Expected: **both PASS.** `test_the_rollback_assertion_can_actually_fail` is what proves the first test's `pg_proc` assertion is live — if the negative control fails, the assertion above it is vacuous and must be fixed before continuing.

- [ ] **Step 3: Fix whatever the rollback test caught**

In descending order of likelihood: a `DROP TABLE` ordering violation against an FK; a missing `DROP FUNCTION`; `CREATE FUNCTION` where `CREATE OR REPLACE FUNCTION` was needed (this fails only on the reapply, at the end of the first test).

- [ ] **Step 4: Run the gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 49, zero skips in `tests/test_db.py` and `tests/test_kb_schema.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_kb_schema.py migrations/
git commit -m "test(kb): execute the rollback, because no down migration ever has been"
```

---

### Task 9: The cutover tripwires

Two tests that protect promises made to `bqa.4`, plus one that is **designed to fail today**. None asserts anything about the schema in isolation; each fails when the world moves.

**Files:** `tests/test_kb_schema.py`

**Interfaces:** Consumes `claims` (Task 4) and `brief_memory` (already imported in the Task 1 header). Produces no schema change.

- [ ] **Step 1: Write the tests**

```python
LEDGER_KEY_TO_COLUMN = {
    "id": "ledger_id",
    "claim": "claim",
    "topic": "topic",
    "first_seen": "first_seen",
    "last_reaffirmed": "last_reaffirmed",
    "restate_count": "restate_count",
    "source_count": "source_count",
    "severity": "severity",
    "origin": "origin",
    "driver": "driver",
    "horizon_days": "horizon_days",
    "resolution_date": "resolution_date",
    "horizon_elapsed": "horizon_elapsed",
    "status": "status",
    "broke_on": "resolved_on",
    "broken_by": "broken_by_note",
    "extractor_model": "extractor_model",
    "prompt_version": "prompt_version",
}

PROVISIONAL_TABLES = {
    "theses",
    "thesis_claims",
    "stories",
    "story_members",
    "open_questions",
    "observations",
    "entity_instruments",
    "links",
}

SCHEMA_STATUSES = {
    "standing",
    "challenged",
    "broken",
    "confirmed",
    "expired",
    "withdrawn",
}


def test_every_ledger_key_has_a_claims_column(kb):
    """Spec 5.1, the WRITE direction. Fails the day a ledger field is added
    without its column -- the drift that would otherwise surface halfway
    through bqa.4.

    IF THIS FAILS: a key was added to brief_memory's claim row. Add the column
    to a new migration and extend this map. Do not delete the entry.
    """
    columns = {
        r[0]
        for r in kb.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'claims'"
        ).fetchall()
    }
    missing = {k: c for k, c in LEDGER_KEY_TO_COLUMN.items() if c not in columns}
    assert not missing, f"ledger keys with no claims column: {missing}"


@pytest.mark.xfail(
    strict=True,
    reason="news-brief-bqa.9: _VALID_STATUS still knows three of the six states. "
    "Remove this marker when bqa.4 widens it.",
)
def test_every_status_the_schema_permits_survives_the_incumbent_coercer():
    """Spec section 6 item 1, the READ direction -- and the one that matters.

    brief_memory._VALID_STATUS knows three values; claims.status permits six.
    _coerce_status returns None for the other three, every caller falls back to
    'standing', and then the TTL filter deletes them after 7 days while
    select_working_set renders them as live fact.

    A write-direction map alone is the-probe-measured-the-wrong-layer, and it
    is exactly what let this defect through two spec reviews.

    strict=True: when bqa.4 fixes _VALID_STATUS this test starts passing, the
    strict xfail turns that into a FAILURE, and whoever sees it removes the
    marker. A plain xfail would go quiet and rot in place.
    """
    degrades = {s for s in SCHEMA_STATUSES if brief_memory._coerce_status(s) != s}
    assert not degrades, (
        f"these statuses coerce away and will be TTL-deleted: {sorted(degrades)}. "
        "Widen brief_memory._VALID_STATUS and split the TTL and render predicates "
        "from '!= standing' to an explicit terminal set (news-brief-bqa.9)."
    )


def test_the_provisional_tables_are_still_empty(kb):
    """Spec 1.2's licence: a provisional table may be reshaped without ceremony
    only while nothing writes it. Nothing observes first write, so this does.

    IF THIS FAILS: bqa.4 has started writing one of these. That is expected and
    good -- go re-read spec 1.2, decide whether the shape is now frozen, and
    narrow PROVISIONAL_TABLES. Do not delete the test.
    """
    non_empty = [
        t
        for t in sorted(PROVISIONAL_TABLES)
        if kb.execute(f"SELECT EXISTS (SELECT 1 FROM {t})").fetchone()[0]
    ]
    assert not non_empty, f"no longer provisional: {non_empty}"
```

- [ ] **Step 2: Run and read the result carefully**

Run: `pytest tests/test_kb_schema.py -q`
Expected: **51 passed, 1 xfailed.** The xfail is `test_every_status_the_schema_permits_survives_the_incumbent_coercer`, and it is correct for it to be failing — it is the tripwire for `news-brief-bqa.9`. If it reports `xpassed` instead, `_VALID_STATUS` has already been widened: remove the marker and the test should pass outright.

- [ ] **Step 3: Run the full gate**

```bash
ruff format . && ruff check . && pytest -q
```
Expected: PASS, count = baseline + 51, one xfailed, zero skips. If `ruff format` rewrites anything, `git add` it before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_kb_schema.py
git commit -m "test(kb): the cutover promises get tripwires, including the one that must fail"
```

- [ ] **Step 5: Close the issue**

```bash
bd close news-brief-bqa.3 --reason="Migration 0006 shipped: 16 tables, 16 indexes, 1 trigger, 51 constraint tests plus an executed rollback."
```

---

## Self-Review

**Spec coverage.** §2.1's nine fields: `source_relationship` Task 2, `metric` Task 3, `entities.type` Task 1, `horizon_days`/`severity`/`status` Task 4, `confidence`/`stories.state`/`links.status` Task 7. §3.4's nine keys: Tasks 1, 2, 4, 5, 7. §4.4 trigger: Task 6. §4.5's sixteen indexes: Tasks 1–7. §5.1 superset: Task 9. §6's cutover contract: item 1 gets the strict-xfail tripwire in Task 9; items 2–6 are `bqa.4` work by definition, tracked as `news-brief-bqa.9`, not implemented here. §7's ten test groups: 1 Task 1, 2–3 Task 8, 4 Tasks 1–7, 5–6 Task 5, 7 Task 4, 8 Task 6, 9–10 Task 9.

**Known gaps, stated rather than hidden.**
1. §4.5's claim that every index has a *named consumer* is not tested. Index existence is checkable; index *use* needs `EXPLAIN`, which tests the planner rather than the schema. Deliberately out.
2. `severity` has no rubric and therefore no rubric test. It is owed by `news-brief-bqa.8`, not by this plan.
3. This plan does **not** consolidate the three module-local `conn` fixtures (`test_db.py:38`, `test_config.py:38`, and the new one). An earlier draft did, and it was wrong: `test_config.py`'s is not a duplicate — it also runs migrations and calls `config.invalidate()` — and a shared unguarded `DROP SCHEMA public CASCADE` fixture would be reachable from every test module by parameter name. File it as its own issue if it ever bites.

**Type consistency.** `_tables`, `rejects`, `_outlet`, `_entity`, `_item`, `_event`, `_observation`, `_claim`, `_story`, `_thesis`, `_functions`, `_copy_migrations` are each defined once, in the task that first needs them, and reused unchanged. `KB_TABLES` (Task 7), `PROVISIONAL_TABLES`, `LEDGER_KEY_TO_COLUMN`, `SCHEMA_STATUSES` (Task 9) are distinct constants. Every import is in the Task 1 header block — E402 is active and there is no ruff config to relax it.

**Sequencing.** Task 1 must fix `test_up_creates_the_expected_tables`'s exact `applied ==` assertion in the same commit that creates `0006`, or the existing suite breaks. Tasks 2–7 each extend that assertion's table set. `claim_evidence` (Task 5) needs `observations` (Task 3) and `claims` (Task 4). `KB_TABLES` and its assertion land in Task 7, not earlier, so that no task ends with a knowingly-red suite — a red suite makes a real regression indistinguishable from a known failure.

**Every expected violation uses `rejects`, never a bare `pytest.raises`.** `db.connect` sets `autocommit=False`, so a violation aborts the transaction and the next statement raises `InFailedSqlTransaction` — a sibling of the expected error, not a subclass. `test_horizon_days_rejects_zero_negatives_and_over_ten_years` would fail on its second iteration without the savepoint.
