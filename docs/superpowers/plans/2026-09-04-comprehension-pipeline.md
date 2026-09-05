# Comprehension Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `comprehend` job that reads captured `items`, decides which are material, and extracts `entities`, `events`, `event_entities` and `assertions` into the knowledge base.

**Architecture:** A supervisor job child, separate from `capture`. Two stages: a triage stage that is a database lookup first and a cheap model call only for what the lookup missed, then an integration stage that resolves entities and events against retrieved candidates before extracting. One model call per micro-batch, one transaction per micro-batch, one savepoint per item.

**Tech Stack:** Python 3.12 (3.14 locally), psycopg 3, Postgres 18, the Anthropic Messages API with forced tool use, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md` — read it alongside this plan. Every task below argues from it, and the spec's §11 revision history records eleven changes made after a red-team pass, several of which reversed the obvious design.

---

## Global Constraints

These apply to **every** task. They are not restated per task.

- **Run the FULL gate before every commit, not just pytest.** CI runs three commands and `ruff format` edits in place:
  ```bash
  ruff check .
  ruff format --check .
  py -m pytest -q
  ```
  `git add` every file `ruff format` touches, or CI fails on the committed tree while your working tree looks clean.
- **A database is required.** `pytest` alone reports green with the entire DB layer skipped, and a skip is not a pass. Before any test run:
  ```bash
  docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief \
    -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test --name nb-test-pg postgres:18-alpine
  until docker exec nb-test-pg pg_isready -U newsbrief > /dev/null 2>&1; do sleep 2; done
  export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
  ```
  The container is often already up from an earlier session — run `docker ps` before starting a second.
- **Use `py`, not `python`.** `py -m pytest -q`, `py -m ruff check .`. The `python` alias is winpty-wrapped and fails with "stdin is not a tty"; `py` is not.
- **Commit from the Bash tool, never PowerShell** — PowerShell prepends a UTF-8 BOM to the commit subject. Use `git commit -F <file>` or repeated `-m` flags; command substitution in a commit message is denied by `windows-guard.sh`, correctly.
- **Stage explicit paths.** Never `git add -A`. The user runs concurrent sessions against this repo, and a file may already be dirty from another one *before* you edit it. Check `git status` on a file before editing it, not only after.
- **The absolute suite count is 1458 as of the spec commit.** Every task states the absolute expected count after it, not a delta.

  **Re-baseline before Task 1 rather than trusting that number.** Run the full gate once, on an unmodified tree, and write down what it reports. Other sessions commit to this repo, so the figure may have moved before you start — and a count that is wrong from the beginning makes every later task's check meaningless in the same direction.

  **When a count disagrees:** first assume the prediction was right and the system is wrong, then find out which. `py -m pytest --collect-only -q` settles the arithmetic; a grep does not. Do not adjust the expected number to match what you observed — that converts a failing check into a passing one without learning anything. Two known-legitimate sources of drift: `tests/test_scheduler.py:259` parametrises over `scheduler.SCHEDULES`, so adding a schedule adds cases; and `tests/test_packaging.py` derives from the real Dockerfile and compose file. If neither explains the gap, stop and report it.
- **Any knob is reached as `common.X`, never `from common import X`.** Knobs are `settings` rows behind a PEP 562 `__getattr__`; a `from`-import binds a copy at import time and the knob can never move.
- **Every absence assertion needs a presence sibling.** `tests-asserting-less-than-their-name` records seven tests in one build that passed while asserting less than their names claimed, and calls it this repo's dominant defect class. For each test you write, mentally delete the code under test and ask whether it would still pass.
- **The test suite blocks non-loopback sockets and FAILS any test that caused a blocked call**, swallowed or not. Every model call in these tests must be stubbed. If you see `Failed: this test reached for the network`, that is a missing stub, not a bad test — do not reach for `@pytest.mark.allow_blocked_network`.
- **Prompt versions:** `TRIAGE_PROMPT_VERSION` and `INTEGRATE_PROMPT_VERSION` both start at `1` and live in `comprehend.py`.

---

## File Structure

| File | Responsibility |
|---|---|
| `migrations/0009_comprehension_up.sql` (create) | `item_triage` table and its two indexes |
| `migrations/0009_comprehension_down.sql` (create) | Reverses the above |
| `comprehend.py` (create) | The whole pipeline: matcher, triage, candidates, integration, run loop, `Tally` |
| `scripts/score_comprehension.py` (create) | The §8 pre-registered gate, read-only |
| `tests/test_comprehension_schema.py` (create) | Constraint tests for 0009 |
| `tests/test_comprehend_matcher.py` (create) | Surface-form matcher rules |
| `tests/test_comprehend_triage.py` (create) | Triage: rules half, model half, sampled arm |
| `tests/test_comprehend_integration.py` (create) | Candidates, parsing, write path, savepoints |
| `common.py` (modify) | Seven `KNOBS` entries |
| `scheduler.py` (modify) | One `Schedule` entry |
| `brief.py` (modify) | `mode_comprehend`, `MODES`, `JOB_MODES` |
| `docker-compose.yml` (modify) | Seven anchor lines |
| `Dockerfile` (modify) | `comprehend.py` on the COPY line |
| `.github/workflows/docker-publish.yml` (modify) | `paths:` filter and both ruff lists |
| `tests/test_db.py` (modify) | One line in the migration manifest |

`comprehend.py` is one module rather than a package because `capture.py` — its sibling job, of comparable scope — is one module, and the repo's Dockerfile COPY allowlist makes each new top-level module a three-place change. Follow the established pattern.

---

## Task 1: Migration 0009 — the triage ledger

**Files:**
- Create: `migrations/0009_comprehension_up.sql`
- Create: `migrations/0009_comprehension_down.sql`
- Create: `tests/test_comprehension_schema.py`
- Modify: `tests/test_db.py:41-50` (the migration manifest)

**Interfaces:**
- Consumes: nothing.
- Produces: table `item_triage` with columns `id, item_id, verdict, reason, triage_model, triage_prompt_version, integrate_prompt_version, attempts, integrate_attempts, integrated_at, created_at`.

- [ ] **Step 1: Write the failing constraint tests**

Create `tests/test_comprehension_schema.py`:

```python
"""Constraint tests for migration 0009 (item_triage).

Every CHECK gets a test that tries to violate it. A constraint nothing
attempts to break is a comment -- the same argument tests/test_kb_schema.py
opens with.
"""

import psycopg
import pytest

import conftest
import db

TARGET = "0009_comprehension"

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        yield c


@pytest.fixture()
def kb(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


def _item(conn) -> int:
    outlet_id = conn.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'H') RETURNING id",
        (outlet_id,),
    ).fetchone()[0]


def _triage(conn, item_id, verdict="material", reason="topical", version=1):
    conn.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s)",
        (item_id, verdict, reason, version),
    )


def test_0009_creates_item_triage(kb):
    rows = kb.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert "item_triage" in {r[0] for r in rows}


def test_verdict_rejects_an_unknown_value(kb):
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="maybe", reason="topical")


def test_reason_rejects_an_unknown_value(kb):
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, reason="vibes")


def test_material_requires_a_real_reason(kb):
    """The biconditional, forward direction: material must not carry 'none'."""
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="material", reason="none")


def test_immaterial_must_not_carry_a_real_reason(kb):
    """The biconditional, REVERSE direction. Without this test the CHECK could
    be written one-way and pass -- the same one-directional blindness
    analysis-stats-traps #3 records in a pre-registered gate."""
    item_id = _item(kb)
    with pytest.raises(psycopg.errors.CheckViolation):
        with kb.transaction():
            _triage(kb, item_id, verdict="immaterial", reason="topical")


def test_a_valid_material_row_is_accepted(kb):
    """Presence sibling for the four rejection tests above: a CHECK that
    rejects everything would satisfy all of them."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="material", reason="tracked_entity")
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 1


def test_sampled_is_a_material_reason(kb):
    """The control arm of spec 5.3 must be storable, and it is material."""
    item_id = _item(kb)
    _triage(kb, item_id, verdict="material", reason="sampled")
    assert kb.execute(
        "SELECT reason FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone()[0] == "sampled"


def test_one_row_per_item_per_triage_version(kb):
    item_id = _item(kb)
    _triage(kb, item_id, version=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with kb.transaction():
            _triage(kb, item_id, version=1)


def test_a_new_triage_version_is_a_new_row_not_an_overwrite(kb):
    """Presence sibling for the uniqueness test: the point of stamping a
    version is to COMPARE versions, which an overwrite destroys."""
    item_id = _item(kb)
    _triage(kb, item_id, version=1)
    _triage(kb, item_id, version=2)
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 2


def test_deleting_an_item_removes_its_triage(kb):
    item_id = _item(kb)
    _triage(kb, item_id)
    kb.execute("DELETE FROM items WHERE id = %s", (item_id,))
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0


def test_0009_rolls_back_and_reapplies(conn):
    """The step count is DERIVED (news-brief-5db). Adding migration 0010 must
    not require an edit here."""
    db.run_migrations(conn)
    conn.commit()
    stack = db.applied_versions(conn)

    reverted = db.run_migrations(
        conn, direction="down", steps=conftest.steps_back_through(conn, TARGET)
    )
    conn.commit()

    assert reverted[-1] == TARGET
    assert reverted[0] == stack[-1]
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert "item_triage" not in tables
    assert "items" in tables, "rolling back 0009 must not take 0006 with it"

    reapplied = db.run_migrations(conn)
    conn.commit()
    assert reapplied == reverted[::-1]
    assert db.applied_versions(conn) == stack
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_comprehension_schema.py -q`
Expected: every test FAILS — `relation "item_triage" does not exist`, and `test_0009_rolls_back_and_reapplies` fails in `steps_back_through` with `0009_comprehension is not applied`.

- [ ] **Step 3: Write the up migration**

Create `migrations/0009_comprehension_up.sql`:

```sql
-- Comprehension pipeline (news-brief-bqa.4b). One row per item per triage
-- prompt version: the verdict, why, and how far integration got.
--
-- A table rather than a column on `items` because deriving "has this been
-- processed" from `assertions` works for integrated items but re-triages every
-- REJECTED item forever, and rejects are the majority.
CREATE TABLE item_triage (
    id             BIGSERIAL PRIMARY KEY,
    item_id        BIGINT  NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    verdict        TEXT    NOT NULL
                   CHECK (verdict IN ('material', 'immaterial', 'failed')),
    -- The union rule has two halves and they are different evidence.
    -- Collapsing them to one bit makes it impossible to ask later whether the
    -- tracked half is carrying the pipeline or the topical half is.
    reason         TEXT    NOT NULL
                   CHECK (reason IN ('tracked_entity', 'tracked_claim',
                                     'tracked_story', 'topical', 'sampled',
                                     'none', 'error')),
    -- Biconditional in BOTH directions, in the style of observations' metric
    -- check. Written one-way it would pass a one-way test while permitting an
    -- immaterial row that claims a real reason.
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error'))),
    -- NULL means the rules half decided and NO model ran. Same reasoning as
    -- observations.provider being distinct from extractor_model: not every row
    -- is produced by a model, and NOT NULL here would force a lie.
    triage_model             TEXT    NULL,
    -- TWO versions, because there are two prompts on two models behind two
    -- knobs. One column served neither: bumping the integration prompt left
    -- integrated_at set so nothing could re-extract, and bumping the triage
    -- prompt re-paid triage cost on items nobody meant to change.
    triage_prompt_version    INTEGER NOT NULL,
    integrate_prompt_version INTEGER NULL,
    attempts                 INTEGER NOT NULL DEFAULT 1,
    -- Integration gets its own ceiling for the reason triage has one: an item
    -- failing deterministically would re-pay the EXPENSIVE tier every run.
    integrate_attempts       INTEGER NOT NULL DEFAULT 0,
    integrated_at            TIMESTAMPTZ NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX item_triage_item_version
    ON item_triage (item_id, triage_prompt_version);

-- Predicated on the verdict as well as the timestamp: immaterial rows never
-- receive integrated_at, so a partial index on that alone would retain roughly
-- 90% rows the only query against it can never return.
CREATE INDEX item_triage_pending ON item_triage (item_id)
    WHERE verdict = 'material' AND integrated_at IS NULL;
```

Create `migrations/0009_comprehension_down.sql`:

```sql
DROP INDEX IF EXISTS item_triage_pending;
DROP INDEX IF EXISTS item_triage_item_version;
DROP TABLE IF EXISTS item_triage;
```

- [ ] **Step 4: Update the migration manifest**

In `tests/test_db.py`, the `applied ==` list in `test_up_creates_the_expected_tables` currently ends `"0008_capture_telemetry",`. Add one line after it:

```python
        "0009_comprehension",
```

This edit is expected and correct: that list is a deliberate inventory that fails loudly. The rollback tests must need **no** edit — that is `news-brief-5db`'s acceptance criterion being exercised for real. If they do need one, stop and report it rather than editing them.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_comprehension_schema.py tests/test_db.py tests/test_kb_schema.py tests/test_claim_store.py -q`
Expected: PASS. `tests/test_comprehension_schema.py` contributes 11 tests.

- [ ] **Step 6: Run the full gate**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1469 passed** (1458 + 11).

- [ ] **Step 7: Commit**

```bash
git add migrations/0009_comprehension_up.sql migrations/0009_comprehension_down.sql \
        tests/test_comprehension_schema.py tests/test_db.py
git commit -F <message-file>
```
Subject: `feat(kb): the triage ledger records why an item was material, and how far it got`

---

## Task 2: Module skeleton, knobs, packaging and job wiring

**Files:**
- Create: `comprehend.py`
- Modify: `common.py` (the `KNOBS` dict, near `"CAPTURE_ENABLED": Knob(bool, False),` at line 264)
- Modify: `scheduler.py` (the `SCHEDULES` tuple)
- Modify: `brief.py` (add `mode_comprehend`, then `MODES` and `JOB_MODES`)
- Modify: `docker-compose.yml` (the `x-newsbrief` anchor)
- Modify: `Dockerfile` (the flat `COPY` line)
- Modify: `.github/workflows/docker-publish.yml` (`paths:` and both ruff lines)
- Create: `tests/test_comprehend_triage.py`

**Interfaces:**
- Consumes: `item_triage` from Task 1.
- Produces: `comprehend.Tally` (dataclass), `comprehend.run(conn) -> Tally`, `comprehend.TRIAGE_PROMPT_VERSION = 1`, `comprehend.INTEGRATE_PROMPT_VERSION = 1`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_comprehend_triage.py`:

```python
"""Triage stage: the rules half, the model half, and the sampled control arm."""

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def test_a_disabled_run_writes_no_triage_rows(kb, monkeypatch):
    """Ships off. The disabled path must be a real no-op, not a path that
    happens to find nothing -- so this test seeds an item that WOULD be
    triaged if the flag were on."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", False)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0
    assert tally.items_seen == 0
    assert tally.enabled is False


def test_an_enabled_run_sees_the_item(kb, monkeypatch):
    """Presence sibling. Without it, the disabled assertion above is satisfied
    for free by a run() that does nothing under any setting."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'Iran signals a ceasefire', 'H')",
        (outlet_id,),
    )
    kb.commit()

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.enabled is True
    assert tally.items_seen == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -m pytest tests/test_comprehend_triage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'comprehend'`.

- [ ] **Step 3: Create the module skeleton**

Create `comprehend.py`:

```python
"""Comprehension: read captured items, decide which are material, extract.

Spec: docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md

A supervisor job child, deliberately SEPARATE from capture. Capture must stay
continuous and cheap because feeds are windows and a missed item is gone
forever; comprehension can be lazy and batched because a captured item can be
reprocessed indefinitely. Concretely, an unbounded model call inside capture's
600s bound would trip the supervisor's overlap alert 48 times a day.

Nothing reads what this writes. entities/events/event_entities/assertions ship
QUARANTINED, per the rule Epic 1 converged on: quarantine is the default for an
unmeasured field, and measurement is what lifts it. The only thing that can say
whether this works is scripts/score_comprehension.py.
"""

import time
from dataclasses import dataclass, field

import common
from common import log

# Bump on any material change to the triage prompt. A prompt change that is not
# versioned is indistinguishable from a change in the world.
TRIAGE_PROMPT_VERSION = 1
INTEGRATE_PROMPT_VERSION = 1

# A pass must not outlive its own fire time: supervisor's _due_jobs loop alerts
# on any job still running at its next fire. Hourly schedule, 40-minute bound.
DEADLINE_SECONDS = 2400


@dataclass
class Tally:
    """What one pass did. Returned AND logged, because a bare count is
    unattributable: 0 rows written is ambiguous across "nothing was captured",
    "everything was immaterial" and "every model call failed"."""

    enabled: bool = False
    items_seen: int = 0
    triaged_by_rules: int = 0
    triaged_by_model: int = 0
    sampled: int = 0
    material: int = 0
    immaterial: int = 0
    failed_triage: int = 0
    failed_integration: int = 0
    gave_up_triage: int = 0
    gave_up_integration: int = 0
    entities_created: int = 0
    entities_resolved: int = 0
    events_created: int = 0
    events_matched: int = 0
    assertions_written: int = 0
    # Distinguishes "one item failed" from "four items were collateral" -- the
    # exact confusion a batch-wide transaction would have produced.
    items_lost_to_savepoint: int = 0
    instrument_entity_refused: int = 0
    candidate_cap_hit: int = 0
    failures: dict = field(default_factory=dict)


def run(conn) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS.

    Commit boundaries are load-bearing: one transaction per micro-batch with a
    savepoint per item. See the module docstring and spec section 6.4.
    """
    tally = Tally(enabled=bool(common.COMPREHEND_ENABLED))
    if not tally.enabled:
        log.info("Comprehend: disabled by COMPREHEND_ENABLED; nothing read")
        return tally

    deadline = time.monotonic() + DEADLINE_SECONDS
    pending = pending_triage(conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS))
    tally.items_seen = len(pending)
    _ = deadline  # stages are added in Tasks 4-10
    log.info(f"Comprehend: {tally}")
    return tally


def pending_triage(conn, version: int, limit: int) -> list[dict]:
    """Items with no verdict at this version, or a retryable failure.

    Oldest first: nothing reads this layer yet, so completeness beats recency
    and no item may starve. Newest-first would permanently skip the tail of any
    backlog.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at "
        "FROM items i "
        "LEFT JOIN item_triage t "
        "  ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.id IS NULL OR (t.verdict = 'failed' AND t.attempts < 3) "
        "ORDER BY i.id "
        "LIMIT %s",
        (version, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
        }
        for r in rows
    ]
```

- [ ] **Step 4: Add the seven knobs**

In `common.py`, immediately after the line `"CAPTURE_ENABLED": Knob(bool, False),`:

```python
    "COMPREHEND_ENABLED": Knob(bool, False),
    "COMPREHEND_TRIAGE_BATCH": Knob(int, 25),
    "COMPREHEND_INTEGRATE_BATCH": Knob(int, 5),
    "COMPREHEND_MAX_ITEMS": Knob(int, 300),
    "COMPREHEND_SAMPLE_PER_DAY": Knob(int, 20),
    "TRIAGE_MODEL": Knob(str, "", env="NEWSBRIEF_TRIAGE_MODEL"),
    "INTEGRATE_MODEL": Knob(str, "", env="NEWSBRIEF_INTEGRATE_MODEL"),
```

**Note the shape of the last two.** The KEY is the attribute name and `env=` carries the prefixed environment variable — matching `"SIGNALS_MODEL": Knob(str, "", env="NEWSBRIEF_SIGNALS_MODEL")` at `common.py:249` and `"MODEL": Knob(str, ..., env="NEWSBRIEF_MODEL")` at `common.py:183`. They are therefore read as `common.TRIAGE_MODEL` and `common.INTEGRATE_MODEL`, **not** `common.TRIAGE_MODEL`. Every reference in Tasks 5, 7 and 10 uses the unprefixed form.

Both default to `""`, never to a copy of the model id. A duplicated literal strands both calls on the old model the moment `NEWSBRIEF_MODEL` moves, silently.

- [ ] **Step 5: Add the compose anchor lines**

In `docker-compose.yml`, immediately after the line `- CAPTURE_ENABLED=${CAPTURE_ENABLED:-}`:

```yaml
    - COMPREHEND_ENABLED=${COMPREHEND_ENABLED:-}
    - COMPREHEND_TRIAGE_BATCH=${COMPREHEND_TRIAGE_BATCH:-}
    - COMPREHEND_INTEGRATE_BATCH=${COMPREHEND_INTEGRATE_BATCH:-}
    - COMPREHEND_MAX_ITEMS=${COMPREHEND_MAX_ITEMS:-}
    - COMPREHEND_SAMPLE_PER_DAY=${COMPREHEND_SAMPLE_PER_DAY:-}
    - NEWSBRIEF_TRIAGE_MODEL=${NEWSBRIEF_TRIAGE_MODEL:-}
    - NEWSBRIEF_INTEGRATE_MODEL=${NEWSBRIEF_INTEGRATE_MODEL:-}
```

The anchor name must equal the `KNOBS` key exactly. `tests/test_packaging.py` enforces this and will fail if it does not.

- [ ] **Step 6: Add `comprehend.py` to all three packaging places**

`Dockerfile`, the flat COPY line — add `comprehend.py` after `capture.py`:
```dockerfile
COPY common.py config.py trading.py polygram_live.py validation.py brief.py capture.py comprehend.py brief_memory.py claim_store.py claim_verify.py retention.py db.py scheduler.py supervisor.py backup.py .
```

`.github/workflows/docker-publish.yml`, the `paths:` filter — add after `- 'capture.py'`:
```yaml
      - 'comprehend.py'
```

The same file's Lint step names files explicitly on **both** lines. Add `comprehend.py` after `capture.py` in each:
```yaml
          ruff check brief.py capture.py comprehend.py brief_memory.py claim_store.py claim_verify.py retention.py common.py config.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py backup.py enrichment scripts tests
          ruff format --check brief.py capture.py comprehend.py brief_memory.py claim_store.py claim_verify.py retention.py common.py config.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py backup.py enrichment scripts tests
```

All three are enforced by `tests/test_packaging.py`. Missing one is how `config.py` reached production absent from the image.

- [ ] **Step 7: Wire the job**

In `scheduler.py`, add to the `SCHEDULES` tuple after the `capture` line:
```python
    Schedule("comprehend", "interval", None, 60, grace_minutes=15),
```

In `brief.py`, immediately after `mode_capture`:
```python
def mode_comprehend():
    """Triage captured items and extract entities, events and assertions.

    Takes no arguments: run_job calls fn() with none. `comprehend.run` owns its
    own commit boundaries -- one transaction per micro-batch, one savepoint per
    item, so a malformed extraction costs one item and not its neighbours.
    """
    import comprehend

    with db.connect() as conn:
        comprehend.run(conn)
```

Then add to `MODES`:
```python
    "comprehend": mode_comprehend,
```

and to `JOB_MODES`:
```python
JOB_MODES = frozenset(
    {"submit", "collect", "weekly", "monitor", "backup", "capture", "comprehend"}
)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `py -m pytest tests/test_comprehend_triage.py tests/test_packaging.py tests/test_scheduler.py -q`
Expected: PASS.

Note `tests/test_scheduler.py:259` parametrises over `scheduler.SCHEDULES`, so adding a schedule silently adds a case. Account for it in the count rather than being surprised by it.

- [ ] **Step 9: Run the full gate**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1472 passed** (1469 + 2 new triage tests + 1 from the `SCHEDULES` parametrisation). If the number differs, run `py -m pytest --collect-only -q` and reconcile before committing.

- [ ] **Step 10: Commit**

```bash
git add comprehend.py common.py scheduler.py brief.py docker-compose.yml Dockerfile \
        .github/workflows/docker-publish.yml tests/test_comprehend_triage.py
git commit -F <message-file>
```
Subject: `feat(comprehend): a job child that ships off and reads nothing yet`

---

## Task 3: The surface-form matcher

**Files:**
- Modify: `comprehend.py`
- Create: `tests/test_comprehend_matcher.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `comprehend.SurfaceForm` (frozen dataclass: `form: str`, `reason: str`, `entity_id: int | None`), `comprehend.form_matches(form: str, text: str) -> bool`, `comprehend.SurfaceIndex` with `.build(conn) -> SurfaceIndex`, `.add_entity(entity_id: int, name: str, aliases: list[str]) -> None`, `.match(text: str) -> list[SurfaceForm]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_comprehend_matcher.py`:

```python
"""Surface-form matching rules.

Every rule here exists because of a recorded failure. PolyGram's substring
search made `MU` match "Musk" (news-brief polygram-candidate-search-fix), and a
geopolitics corpus answers "who is DISCUSSED" when you ask "who is PRESENT".
These are not stylistic choices.
"""

import comprehend


def test_a_long_form_matches_case_insensitively():
    assert comprehend.form_matches("Ukraine", "ukraine signals a ceasefire")


def test_a_long_form_matches_on_a_word_boundary():
    assert comprehend.form_matches("Iran", "Iran and Israel met today")


def test_a_long_form_does_NOT_match_inside_a_word():
    """Substring matching is the recorded PolyGram bug. 'Iran' must not match
    'Iranian-adjacent' via a bare `in` check -- it matches here only because a
    hyphen is a word boundary, so use a word that truly embeds it."""
    assert not comprehend.form_matches("Iran", "the tiranian delegation")


def test_a_short_form_matches_case_SENSITIVELY():
    """Acronyms are real entities. Casefolding them is what makes them toxic:
    lowercased, `US` matches the pronoun in every second sentence."""
    assert comprehend.form_matches("US", "The US said today")
    assert not comprehend.form_matches("US", "he told us today")


def test_a_short_form_still_needs_a_word_boundary():
    assert not comprehend.form_matches("US", "USB drives were seized")


def test_html_entities_are_decoded_and_whitespace_collapsed():
    """Measured on production 2026-09-05: bodies carry literal &nbsp;, e.g.
    `...facing 10 years in jail&nbsp;&nbsp;Reuters`."""
    assert comprehend.clean("jail&nbsp;&nbsp;Reuters") == "jail Reuters"


def test_a_MULTI_WORD_form_needs_the_decode_to_match():
    """This is where the decode earns its place, and the single-word case is
    NOT the example: `&nbsp;` ends in a semicolon, which is already a word
    boundary, so `Reuters` matches with or without it. A form containing a
    SPACE is what breaks -- the entity sits where the space should be.
    """
    raw = "the New&nbsp;York talks resumed"
    assert not comprehend.form_matches("New York", raw), (
        "undecoded, the space in the form cannot match the entity in the text"
    )
    assert comprehend.form_matches("New York", comprehend.clean(raw)), (
        "presence sibling: decoded, the same form matches the same text"
    )


def test_a_stop_listed_form_never_matches():
    assert "will" in comprehend.STOP_FORMS
    assert not comprehend.form_matches("will", "the deal will collapse")


def test_a_regex_metacharacter_in_a_name_is_matched_literally():
    """Entity names contain dots and parentheses. An unescaped form would
    either crash or match far too much."""
    assert comprehend.form_matches("U.S.", "U.S. officials confirmed")
    assert not comprehend.form_matches("U.S.", "UXSX officials confirmed")


def test_the_index_matches_an_entity_by_alias():
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Volodymyr Zelenskyy", ["Zelensky", "Zelenskyy"])
    hits = index.match("Zelensky met the delegation")
    assert [h.entity_id for h in hits] == [7]
    assert hits[0].reason == "tracked_entity"


def test_the_index_returns_nothing_for_an_unrelated_text():
    """Absence assertion. Its presence sibling is the test above -- without
    one, an index that always returns [] passes this."""
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Volodymyr Zelenskyy", ["Zelensky"])
    assert index.match("chip export controls tightened") == []


def test_the_index_deduplicates_one_entity_matched_twice():
    index = comprehend.SurfaceIndex([])
    index.add_entity(7, "Zelensky", ["Zelenskyy"])
    hits = index.match("Zelensky and Zelenskyy are the same person")
    assert len({h.entity_id for h in hits}) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_comprehend_matcher.py -q`
Expected: FAIL — `module 'comprehend' has no attribute 'form_matches'`.

- [ ] **Step 3: Implement the matcher**

**Add `import html` and `import re` to the TOP import block of `comprehend.py`**, beside `import time`. Do not append it lower down: ruff's E402 is active (there is no `pyproject.toml` or `ruff.toml` in this repo, so defaults apply), and a mid-file module-level import fails `ruff check .` with exit 1. E402 constrains an import's POSITION, not when it is added — the move that satisfies it is always editing the top block.

Then add to `comprehend.py`, after `pending_triage`:

```python
# Surface forms that are also ordinary English words. A form on this list never
# matches, whatever entity claims it. Short and hand-maintained on purpose: a
# large stop-list hides a matcher that is too loose.
STOP_FORMS = frozenset(
    {
        "will", "may", "can", "us", "it", "he", "she", "they", "the", "and",
        "for", "was", "are", "has", "had", "new", "one", "two", "all", "any",
    }
)

# Below this length a form must match case-sensitively. Acronyms are real
# entities (US, EU, UN, IMF); lowercased they collide with common words.
_CASE_SENSITIVE_BELOW = 4


def clean(text: str | None) -> str:
    """Decode HTML entities and collapse whitespace.

    Measured on production 2026-09-05: item bodies carry literal `&nbsp;`.
    Reuters items read `...facing 10 years in jail&nbsp;&nbsp;Reuters`. Without
    unescaping, a surface form spanning an entity boundary silently fails to
    match -- and a silent miss in the tracked half presents as "the KB did not
    find that interesting", not as an error.

    Used by BOTH the matcher and the prompt builders, so the model never sees
    entity noise either.
    """
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def form_matches(form: str, text: str) -> bool:
    """Word-boundary match, never substring.

    Substring matching is the recorded PolyGram failure: `MU` matched "Musk".
    """
    form = (form or "").strip()
    if not form or not text:
        return False
    # The stop-list check is CASE-SENSITIVE for short forms, and that is not a
    # detail. STOP_FORMS holds lowercase words, and a short form already relies
    # on case to disambiguate -- so lowercasing before the lookup would stop
    # `US` (the country) because `us` (the pronoun) is on the list, and the same
    # for EU, UN and every other acronym that is also a common word. The short
    # form `us` is still stopped; the entity `US` is not.
    if (form if len(form) < _CASE_SENSITIVE_BELOW else form.lower()) in STOP_FORMS:
        return False
    flags = 0 if len(form) < _CASE_SENSITIVE_BELOW else re.IGNORECASE
    return re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text, flags) is not None


@dataclass(frozen=True)
class SurfaceForm:
    form: str
    reason: str  # tracked_entity | tracked_claim | tracked_story
    entity_id: int | None = None


class SurfaceIndex:
    """Every trackable surface form, held in memory for one run.

    A full scan of `entities` per run, deliberately: the word-boundary and
    case rules above do not express well in SQL, and one scan per run is
    cheaper than a per-mention query. This is why there is NO GIN index on
    entities.aliases -- an index with no reader is dead weight.
    """

    def __init__(self, forms: list[SurfaceForm]) -> None:
        self._forms = list(forms)

    @classmethod
    def build(cls, conn) -> "SurfaceIndex":
        forms: list[SurfaceForm] = []
        for eid, name, aliases in conn.execute(
            "SELECT id, name, aliases FROM entities"
        ).fetchall():
            for f in [name, *(aliases or [])]:
                forms.append(SurfaceForm(f, "tracked_entity", eid))
        for (topic,) in conn.execute(
            "SELECT DISTINCT topic FROM claims "
            "WHERE topic IS NOT NULL AND status IN ('standing', 'challenged')"
        ).fetchall():
            forms.append(SurfaceForm(topic, "tracked_claim", None))
        for (name,) in conn.execute(
            "SELECT name FROM stories WHERE state <> 'closed'"
        ).fetchall():
            forms.append(SurfaceForm(name, "tracked_story", None))
        return cls(forms)

    def add_entity(self, entity_id: int, name: str, aliases: list[str]) -> None:
        """Called after each batch's writes. Built once per run, an entity
        created in batch 1 is invisible to batch 3, so events attached to it
        cannot be offered as candidates -- which depresses events_matched, the
        numerator of the corroboration gate. The gate would then fail for a
        caching reason while the matcher worked."""
        for f in [name, *(aliases or [])]:
            self._forms.append(SurfaceForm(f, "tracked_entity", entity_id))

    def match(self, text: str) -> list[SurfaceForm]:
        """Every distinct hit, deduplicated by entity (or by form where there
        is no entity). Order is stable: entity hits first, in insertion order."""
        seen: set = set()
        out: list[SurfaceForm] = []
        for sf in self._forms:
            key = ("e", sf.entity_id) if sf.entity_id is not None else ("f", sf.form)
            if key in seen:
                continue
            if form_matches(sf.form, text):
                seen.add(key)
                out.append(sf)
        return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_comprehend_matcher.py -q`
Expected: PASS, 10 tests.

- [ ] **Step 5: Run the full gate**

Expected: **1484 passed** (1472 + 12).

- [ ] **Step 6: Commit**

```bash
git add comprehend.py tests/test_comprehend_matcher.py
git commit -F <message-file>
```
Subject: `feat(comprehend): word-boundary matching, because MU once matched Musk`

---

## Task 4: Triage rules half

**Files:**
- Modify: `comprehend.py`
- Modify: `tests/test_comprehend_triage.py`

**Interfaces:**
- Consumes: `SurfaceIndex`, `pending_triage`, `Tally`.
- Produces: `comprehend.record_triage(conn, item_id, verdict, reason, triage_model, version) -> None`, `comprehend.triage_by_rules(item: dict, index: SurfaceIndex) -> SurfaceForm | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_comprehend_triage.py`:

```python
def _outlet(kb, name="Reuters"):
    """Get-or-create. `outlets` is UNIQUE (lower(name)) (0006:25), so a helper
    that always inserts raises UniqueViolation the second time a test calls it
    -- and several tests below add multiple items."""
    row = kb.execute(
        "SELECT id FROM outlets WHERE lower(name) = lower(%s)", (name,)
    ).fetchone()
    if row:
        return row[0]
    return kb.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _add_item(kb, title, body=None, outlet_id=None, h="H1"):
    """`items` is UNIQUE (outlet_id, content_hash), so callers adding more than
    one item to the same outlet must pass distinct `h`."""
    outlet_id = outlet_id or _outlet(kb)
    return kb.execute(
        "INSERT INTO items (outlet_id, url, title, body, content_hash, published_at) "
        "VALUES (%s, 'u', %s, %s, %s, now()) RETURNING id",
        (outlet_id, title, body, h),
    ).fetchone()[0]


def test_a_tracked_entity_makes_an_item_material_with_no_model_call(kb):
    kb.execute(
        "INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')"
    )
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    hit = comprehend.triage_by_rules(item, index)

    assert hit is not None
    assert hit.reason == "tracked_entity"
    comprehend.record_triage(
        kb, item_id, "material", hit.reason, None, comprehend.TRIAGE_PROMPT_VERSION
    )
    kb.commit()
    row = kb.execute(
        "SELECT verdict, reason, triage_model FROM item_triage WHERE item_id = %s",
        (item_id,),
    ).fetchone()
    assert row == ("material", "tracked_entity", None), (
        "triage_model must be NULL when no model ran -- a NOT NULL value here "
        "would make the rules half indistinguishable from the model half"
    )


def test_an_untracked_item_is_not_matched_by_the_rules(kb):
    """Absence assertion; the test above is its presence sibling."""
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    item_id = _add_item(kb, "Chip export controls tightened")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    assert comprehend.triage_by_rules(item, index) is None
    assert item_id  # the item exists; it simply did not match


def test_the_rules_half_reads_the_body_as_well_as_the_title(kb):
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    _add_item(kb, "A ceasefire is signalled", body="Officials in Ukraine confirmed.")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]
    assert comprehend.triage_by_rules(item, index) is not None


def test_a_live_claim_topic_is_tracked_but_a_terminal_one_is_not(kb):
    # first_seen is DATE NOT NULL with no default (0006:193): omitting it
    # raises NotNullViolation. last_reaffirmed is nullable (0006:194) but is
    # supplied anyway, because claim_store._row_to_claim treats a NULL there as
    # a HARD ERROR -- the column's nullability and the loader's contract
    # disagree, and a fixture that exercises the disagreement will confuse
    # whoever debugs it next.
    kb.execute(
        "INSERT INTO claims (claim, topic, status, first_seen, last_reaffirmed) "
        "VALUES ('c', 'Sahel', 'standing', CURRENT_DATE, CURRENT_DATE)"
    )
    kb.execute(
        "INSERT INTO claims (claim, topic, status, resolved_on, first_seen, "
        "  last_reaffirmed) "
        "VALUES ('d', 'Balkans', 'withdrawn', CURRENT_DATE, CURRENT_DATE, CURRENT_DATE)"
    )
    kb.commit()
    index = comprehend.SurfaceIndex.build(kb)

    assert [f.reason for f in index.match("Sahel unrest deepens")] == ["tracked_claim"]
    assert index.match("Balkans unrest deepens") == [], (
        "a terminal claim's topic must not be tracked, or every settled "
        "question stays permanently material"
    )


def test_a_triaged_item_is_not_returned_again_at_the_same_version(kb):
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()
    assert len(comprehend.pending_triage(kb, 1, 10)) == 1

    comprehend.record_triage(kb, item_id, "immaterial", "none", None, 1)
    kb.commit()

    assert comprehend.pending_triage(kb, 1, 10) == []
    assert len(comprehend.pending_triage(kb, 2, 10)) == 1, (
        "but a NEW triage prompt version must see it again"
    )


def test_a_failed_item_is_retried_until_the_ceiling(kb):
    item_id = _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()
    comprehend.record_triage(kb, item_id, "failed", "error", None, 1)
    kb.commit()
    assert len(comprehend.pending_triage(kb, 1, 10)) == 1, "attempts=1 is retryable"

    kb.execute("UPDATE item_triage SET attempts = 3 WHERE item_id = %s", (item_id,))
    kb.commit()
    assert comprehend.pending_triage(kb, 1, 10) == [], (
        "at the ceiling it stops being selected, or it re-pays model cost forever"
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_comprehend_triage.py -q`
Expected: FAIL — `module 'comprehend' has no attribute 'triage_by_rules'`.

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
def triage_by_rules(item: dict, index: SurfaceIndex) -> SurfaceForm | None:
    """The tracked half. A database lookup, no model call.

    Reads title AND body. The body is the RSS blurb -- capture.py stores
    entry.get("summary"), not article text -- so it is short and there is no
    window to choose.
    """
    text = f"{clean(item.get('title'))} {clean(item.get('body'))}"
    hits = index.match(text)
    return hits[0] if hits else None


def record_triage(conn, item_id, verdict, reason, triage_model, version) -> None:
    """Insert a verdict, or bump `attempts` on a retry at the same version.

    ON CONFLICT rather than a plain INSERT because a failed row is retried, and
    the unique key would otherwise make the first failure permanent until
    someone bumped the prompt version.
    """
    conn.execute(
        "INSERT INTO item_triage "
        "  (item_id, verdict, reason, triage_model, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, triage_prompt_version) DO UPDATE SET "
        "  verdict = EXCLUDED.verdict, reason = EXCLUDED.reason, "
        "  triage_model = EXCLUDED.triage_model, "
        "  attempts = item_triage.attempts + 1",
        (item_id, verdict, reason, triage_model, version),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_comprehend_triage.py -q`
Expected: PASS, 8 tests in the file.

- [ ] **Step 5: Full gate.** Expected: **1490 passed** (1484 + 6).

- [ ] **Step 6: Commit**

Subject: `feat(comprehend): the tracked half is a lookup, so it costs no tokens`

---

## Task 5: Triage model half

**Files:**
- Modify: `comprehend.py`
- Modify: `tests/test_comprehend_triage.py`

**Interfaces:**
- Consumes: `pending_triage`, `record_triage`.
- Produces: `comprehend.build_triage_request(items: list[dict]) -> dict`, `comprehend.parse_triage_response(resp: dict, offered_ids: set[int]) -> dict[int, bool]`, `comprehend._triage_model() -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_comprehend_triage.py`:

```python
def _tool_use(items):
    return {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "emit_triage",
                "input": {"items": items},
            }
        ],
    }


def test_the_triage_request_forces_the_tool_and_disables_thinking():
    req = comprehend.build_triage_request(
        [{"id": 1, "title": "t", "body": "b", "outlet": "Reuters"}]
    )
    assert req["tool_choice"] == {"type": "tool", "name": "emit_triage"}
    assert req["thinking"] == {"type": "disabled"}, (
        "a forced-tool extraction on a tight budget must disable thinking: "
        "Sonnet 5 runs ADAPTIVE thinking when it is omitted, which eats "
        "max_tokens and truncates"
    )


def test_a_truncated_response_raises_rather_than_being_parsed():
    """stop_reason is checked BEFORE the parser. Four recorded recurrences of
    a truncation being misdiagnosed as a broken parser."""
    resp = _tool_use([{"id": 1, "material": True}])
    resp["stop_reason"] = "max_tokens"
    with pytest.raises(ValueError, match="truncated"):
        comprehend.parse_triage_response(resp, {1})


def test_a_well_formed_response_is_parsed():
    """Presence sibling for the truncation test: an always-raising parser
    would satisfy that one for free."""
    resp = _tool_use([{"id": 1, "material": True}, {"id": 2, "material": False}])
    assert comprehend.parse_triage_response(resp, {1, 2}) == {1: True, 2: False}


def test_an_id_that_was_never_offered_is_dropped():
    """The model can return an id we did not send. Trusting it would write a
    verdict against an unrelated item."""
    resp = _tool_use([{"id": 1, "material": True}, {"id": 999, "material": True}])
    assert comprehend.parse_triage_response(resp, {1}) == {1: True}


def test_a_missing_tool_block_raises():
    with pytest.raises(ValueError, match="emit_triage"):
        comprehend.parse_triage_response({"stop_reason": "end_turn", "content": []}, {1})
```

- [ ] **Step 2: Run to verify they fail.** Expected: `no attribute 'build_triage_request'`.

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
# Sized from the batch: 25 items x ~25 output tokens plus schema overhead. A
# tight max_tokens is what truncated the signals call twice; leave headroom and
# check stop_reason regardless.
TRIAGE_MAX_TOKENS = 2048

_TRIAGE_TOOL = {
    "name": "emit_triage",
    "description": "Return one materiality verdict per input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "material": {"type": "boolean"},
                    },
                    "required": ["id", "material"],
                },
            }
        },
        "required": ["items"],
    },
}

_TRIAGE_SYSTEM = """You are triaging news items for a geopolitics and macro \
knowledge base. For each item, answer one question: does it belong to that \
domain -- international relations, conflict, statecraft, energy, trade, \
central banks, sovereign risk, or the markets those move?

Judge the SUBJECT, not the importance. A minor development in the domain is \
material; a major story outside it is not. Sport, entertainment, crime and \
consumer news are not material unless they carry a stated geopolitical or \
macro consequence.

Return a verdict for every id you were given, and no others."""


def _triage_model() -> str:
    """An unset NEWSBRIEF_TRIAGE_MODEL means "follow MODEL"."""
    return common.TRIAGE_MODEL or common.MODEL


def build_triage_request(items: list[dict]) -> dict:
    lines = []
    for it in items:
        # clean() so the model never sees `&nbsp;` noise either. Measured
        # 2026-09-05: 41% of captured volume is Google News proxy items whose
        # body is the headline restated with entity separators.
        body = clean(it.get("body"))
        lines.append(
            f"- id={it['id']} outlet={it.get('outlet', '?')} "
            f"title={clean(it['title'])!r} lead={body[:300]!r}"
        )
    return {
        "model": _triage_model(),
        "max_tokens": TRIAGE_MAX_TOKENS,
        # Forced-tool extraction on a tight budget: thinking OFF. Omitting the
        # field runs ADAPTIVE thinking on Sonnet 5, which spends max_tokens.
        "thinking": {"type": "disabled"},
        "system": _TRIAGE_SYSTEM,
        "tools": [_TRIAGE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_triage"},
        "messages": [{"role": "user", "content": "ITEMS:\n" + "\n".join(lines)}],
    }


def parse_triage_response(resp: dict, offered_ids: set[int]) -> dict[int, bool]:
    """id -> material. Drops ids that were never offered.

    stop_reason is checked FIRST: a truncated reply produces a parse error that
    reads as a broken parser, and this repo has misdiagnosed that four times.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("triage response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_triage":
            rows = block.get("input", {}).get("items")
            if not isinstance(rows, list):
                raise ValueError("emit_triage input missing 'items' list")
            out: dict[int, bool] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rid, mat = r.get("id"), r.get("material")
                if isinstance(rid, int) and isinstance(mat, bool) and rid in offered_ids:
                    out[rid] = mat
            return out
    raise ValueError("no emit_triage tool_use block in response")
```

- [ ] **Step 4: Run to verify they pass.** 13 tests in the file.

- [ ] **Step 5: Full gate.** Expected: **1495 passed** (1490 + 5).

- [ ] **Step 6: Commit**

Subject: `feat(comprehend): topical triage, with stop_reason checked before the parser`

---

## Task 6: The sampled control arm

**Files:**
- Modify: `comprehend.py`
- Modify: `tests/test_comprehend_triage.py`

**Interfaces:**
- Consumes: `record_triage`.
- Produces: `comprehend.select_sampled(conn, version: int, per_day: int) -> list[int]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_comprehend_triage.py`:

```python
def test_the_sample_draws_only_from_items_both_halves_rejected(kb):
    rejected = _add_item(kb, "Local sports result", h="H1")
    accepted = _add_item(kb, "Ukraine ceasefire", h="H2")
    kb.commit()
    comprehend.record_triage(kb, rejected, "immaterial", "none", "m", 1)
    comprehend.record_triage(kb, accepted, "material", "topical", "m", 1)
    kb.commit()

    assert comprehend.select_sampled(kb, 1, 10) == [rejected], (
        "a material item is already integrated; sampling it would not be a "
        "selection-independent control"
    )


def test_the_sample_respects_the_daily_cap(kb):
    ids = [_add_item(kb, f"Item {i}", h=f"H{i}") for i in range(5)]
    kb.commit()
    for i in ids:
        comprehend.record_triage(kb, i, "immaterial", "none", "m", 1)
    kb.commit()

    assert len(comprehend.select_sampled(kb, 1, 2)) == 2


def test_an_already_sampled_item_is_not_sampled_again(kb):
    item_id = _add_item(kb, "Local sports result")
    kb.commit()
    comprehend.record_triage(kb, item_id, "immaterial", "none", "m", 1)
    kb.commit()
    assert comprehend.select_sampled(kb, 1, 10) == [item_id]

    comprehend.record_triage(kb, item_id, "material", "sampled", None, 1)
    kb.commit()
    assert comprehend.select_sampled(kb, 1, 10) == []
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
def select_sampled(conn, version: int, per_day: int) -> list[int]:
    """Items BOTH halves rejected, promoted to material as the §5.3 control.

    Why this exists, and it is not coverage: a model-judged `topical` set is
    confounded with the triage model's own view of the domain. Measure enum
    variance over topical rows alone and there is no way to separate "the
    extractor works" from "triage picked items its sibling finds easy". A
    recency sample is not confounded, so these rows are the control the other
    arms are read against.

    Cheap because it is capped: removing the model triage half entirely and
    integrating by recency was considered and rejected -- it moves integration
    from ~120/day to ~1,200/day in the expensive tier.

    PER DAY, NOT PER RUN. The schedule is hourly, so a bare LIMIT would draw
    the cap on every fire: 20 becomes 480/day in the EXPENSIVE tier, turning a
    ~17% control-arm overhead into ~400% -- the same order of cost error as the
    proposal this arm was chosen over.
    """
    used = conn.execute(
        "SELECT count(*) FROM item_triage "
        "WHERE reason = 'sampled' AND created_at >= date_trunc('day', now())"
    ).fetchone()[0]
    remaining = max(0, per_day - used)
    if remaining == 0:
        return []
    rows = conn.execute(
        "SELECT item_id FROM item_triage "
        "WHERE triage_prompt_version = %s AND verdict = 'immaterial' "
        "ORDER BY item_id DESC LIMIT %s",
        (version, remaining),
    ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Full gate.** Expected: **1498 passed** (1495 + 3).
- [ ] **Step 6: Commit**

Subject: `feat(comprehend): a sampled arm, because topical rows cannot control for themselves`

---

## Task 7: Candidate retrieval

**Files:**
- Modify: `comprehend.py`
- Create: `tests/test_comprehend_integration.py`

**Interfaces:**
- Consumes: `SurfaceIndex`.
- Produces: `comprehend.CANDIDATE_ENTITY_CAP = 40`, `comprehend.CANDIDATE_EVENT_CAP = 30`, `comprehend.CANDIDATE_WINDOW_DAYS = 14`, `comprehend.candidate_events(conn, entity_ids: list[int], tally: Tally) -> list[dict]`, `comprehend.build_integration_request(items: list[dict], entities: list[dict], events: list[dict]) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_comprehend_integration.py`:

```python
"""Integration stage: candidates, parsing, and the savepoint write path."""

import json

import pytest

import comprehend
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def _entity(kb, name="Ukraine", type_="country"):
    return kb.execute(
        "INSERT INTO entities (name, type) VALUES (%s, %s) RETURNING id", (name, type_)
    ).fetchone()[0]


def _event(kb, entity_id, summary="A ceasefire was announced", days_ago=1):
    eid = kb.execute(
        "INSERT INTO events (summary, type, commitment_state, occurred_at) "
        "VALUES (%s, 'statement', 'intended', now() - make_interval(days => %s)) "
        "RETURNING id",
        (summary, days_ago),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO event_entities (event_id, entity_id) VALUES (%s, %s)",
        (eid, entity_id),
    )
    return eid


def test_candidate_events_are_found_through_shared_entities(kb):
    ent = _entity(kb)
    ev = _event(kb, ent)
    kb.commit()
    got = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    assert [c["id"] for c in got] == [ev]


def test_candidate_events_outside_the_window_are_excluded(kb):
    ent = _entity(kb)
    _event(kb, ent, days_ago=comprehend.CANDIDATE_WINDOW_DAYS + 5)
    kb.commit()
    assert comprehend.candidate_events(kb, [ent], comprehend.Tally()) == []


def test_candidate_events_carry_ONLY_id_and_summary(kb):
    """Load-bearing, and the gate depends on it.

    analysis-stats-traps #4: the gold-set probe handed the model each row's
    severity already populated, then reported severity variance as evidence the
    field was healthy -- unchanged on 21 of 23, because the variance was ECHO.
    scripts/score_comprehension.py scores events.type and commitment_state, so
    a candidate carrying those primes every assertion attached to it and the
    gate measures the prompt instead of the model, while still reading as a
    pass.
    """
    ent = _entity(kb)
    _event(kb, ent)
    kb.commit()
    got = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    assert set(got[0]) == {"id", "summary"}


def test_the_integration_prompt_never_mentions_a_candidate_enum_value(kb):
    """The same rule enforced at the layer that actually reaches the model.
    The test above pins the retrieval shape; this pins what is SENT, which is
    the thing that would silently invalidate the measurement."""
    ent = _entity(kb)
    _event(kb, ent)
    kb.commit()
    events = comprehend.candidate_events(kb, [ent], comprehend.Tally())
    req = comprehend.build_integration_request(
        [{"id": 1, "title": "t", "body": "b", "outlet": "Reuters"}],
        [{"id": ent, "name": "Ukraine", "type": "country"}],
        events,
    )
    sent = json.dumps(req["messages"])
    assert "intended" not in sent, "a candidate's commitment_state leaked into the prompt"
    assert "A ceasefire was announced" in sent, (
        "presence sibling: the summary MUST be sent, or the absence assertion "
        "above passes for a prompt that sends no candidates at all"
    )


def test_hitting_the_candidate_cap_is_counted(kb):
    ent = _entity(kb)
    for i in range(comprehend.CANDIDATE_EVENT_CAP + 3):
        _event(kb, ent, summary=f"Event {i}")
    kb.commit()
    tally = comprehend.Tally()
    got = comprehend.candidate_events(kb, [ent], tally)
    assert len(got) == comprehend.CANDIDATE_EVENT_CAP
    assert tally.candidate_cap_hit == 1
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
# Prompt-budget choices, not measurements. Ordered most-recent-first so
# truncation drops the least likely candidates, and a cap reached is counted.
CANDIDATE_ENTITY_CAP = 40
CANDIDATE_EVENT_CAP = 30
CANDIDATE_WINDOW_DAYS = 14

INTEGRATE_MAX_TOKENS = 8192


def candidate_events(conn, entity_ids: list[int], tally: Tally) -> list[dict]:
    """Recent events sharing an entity with this batch.

    Returns id and summary and NOTHING ELSE. events.type and
    commitment_state are scored by the pre-registered gate; sending them here
    would make every attached assertion inherit the framing by echo, and the
    gate would measure the prompt rather than the model.
    """
    if not entity_ids:
        return []
    rows = conn.execute(
        "SELECT DISTINCT e.id, e.summary, e.occurred_at FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id "
        "WHERE ee.entity_id = ANY(%s) "
        "  AND e.occurred_at >= now() - make_interval(days => %s) "
        "ORDER BY e.occurred_at DESC "
        "LIMIT %s",
        (list(entity_ids), CANDIDATE_WINDOW_DAYS, CANDIDATE_EVENT_CAP + 1),
    ).fetchall()
    if len(rows) > CANDIDATE_EVENT_CAP:
        tally.candidate_cap_hit += 1
        rows = rows[:CANDIDATE_EVENT_CAP]
    return [{"id": r[0], "summary": r[1]} for r in rows]


_INTEGRATE_SYSTEM = """You extract structured knowledge from news items for a \
geopolitics and macro knowledge base.

For each item, return:
  entities   -- the actors involved. Prefer an id from CANDIDATE ENTITIES when \
one refers to the same real-world actor; otherwise propose a new entity.
  events     -- what happened. Prefer an id from CANDIDATE EVENTS when the item \
reports the SAME event another outlet already reported; otherwise propose a new \
one. Matching an existing event is how corroboration is recorded, so match \
whenever the underlying occurrence is the same, even if the wording differs.
  assertion  -- how this outlet stands behind each event.

ONE ENTITY PER REAL-WORLD ACTOR. A company and its equity line are the SAME \
entity: use type 'company' and put the ticker in aliases. Never create a \
separate entity of type 'instrument' for a company's shares.

Distinguish what was DONE from what was SAID. "Trump declared the ceasefire \
over" is a statement; whether the ceasefire is over is a separate matter."""

_INTEGRATE_TOOL = {
    "name": "emit_extraction",
    "description": "Structured extraction for each input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer"},
                        "entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate_id": {"type": "integer"},
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "country", "institution", "company",
                                            "person", "instrument",
                                        ],
                                    },
                                    "aliases": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "events": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate_id": {"type": "integer"},
                                    "summary": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": ["action", "statement", "disclosure"],
                                    },
                                    "commitment_state": {
                                        "type": "string",
                                        "enum": [
                                            "in_force", "committed",
                                            "intended", "proposed",
                                        ],
                                    },
                                    "standing": {
                                        "type": "string",
                                        "enum": [
                                            "verified", "official", "reported",
                                            "attributed", "alleged",
                                        ],
                                    },
                                },
                                "required": ["standing"],
                            },
                        },
                    },
                    "required": ["item_id", "entities", "events"],
                },
            }
        },
        "required": ["items"],
    },
}


def _integrate_model() -> str:
    return common.INTEGRATE_MODEL or common.MODEL


def build_integration_request(
    items: list[dict], entities: list[dict], events: list[dict]
) -> dict:
    ent_lines = "\n".join(
        f"- id={e['id']} {e['name']} ({e['type']})" for e in entities
    ) or "(none)"
    # id and summary ONLY. See candidate_events.
    ev_lines = "\n".join(f"- id={e['id']} {e['summary']}" for e in events) or "(none)"
    item_lines = "\n".join(
        f"- item_id={it['id']} outlet={it.get('outlet', '?')} "
        f"title={clean(it['title'])!r}\n  body={clean(it.get('body'))!r}"
        for it in items
    )
    return {
        "model": _integrate_model(),
        "max_tokens": INTEGRATE_MAX_TOKENS,
        "thinking": {"type": "disabled"},
        "system": _INTEGRATE_SYSTEM,
        "tools": [_INTEGRATE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_extraction"},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"CANDIDATE ENTITIES:\n{ent_lines}\n\n"
                    f"CANDIDATE EVENTS:\n{ev_lines}\n\n"
                    f"ITEMS:\n{item_lines}"
                ),
            }
        ],
    }
```

Note `assertions.source_relationship` is absent from the tool schema. It is not extracted in v1: `bqa.8` records it as the only extracted enum with no worked example anywhere, which is `severity`'s exact provenance.

- [ ] **Step 4: Run to verify they pass.** 5 tests.
- [ ] **Step 5: Full gate.** Expected: **1503 passed** (1498 + 5).
- [ ] **Step 6: Commit**

Subject: `feat(comprehend): candidates offer a summary and withhold the label being scored`

---

## Task 8: Parse the extraction

**Files:**
- Modify: `comprehend.py`
- Modify: `tests/test_comprehend_integration.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `comprehend.parse_integration_response(resp: dict, offered_item_ids: set[int], offered_entity_ids: set[int], offered_event_ids: set[int]) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_comprehend_integration.py`:

```python
def _extraction(items):
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "emit_extraction", "input": {"items": items}}
        ],
    }


ONE = {
    "item_id": 1,
    "entities": [{"candidate_id": 10}],
    "events": [{"candidate_id": 20, "standing": "reported"}],
}


def test_a_well_formed_extraction_is_parsed():
    got = comprehend.parse_integration_response(_extraction([ONE]), {1}, {10}, {20})
    assert got[0]["item_id"] == 1
    assert got[0]["entities"][0]["candidate_id"] == 10


def test_a_truncated_extraction_raises_before_parsing():
    resp = _extraction([ONE])
    resp["stop_reason"] = "max_tokens"
    with pytest.raises(ValueError, match="truncated"):
        comprehend.parse_integration_response(resp, {1}, {10}, {20})


def test_a_hallucinated_entity_candidate_id_is_rejected():
    """The model can name an id that was never offered. Writing it would
    attach this item's assertion to an unrelated entity."""
    bad = dict(ONE, entities=[{"candidate_id": 999}])
    got = comprehend.parse_integration_response(_extraction([bad]), {1}, {10}, {20})
    assert got == [], "an item citing an unoffered entity id must be dropped whole"


def test_a_hallucinated_event_candidate_id_is_rejected():
    bad = dict(ONE, events=[{"candidate_id": 999, "standing": "reported"}])
    got = comprehend.parse_integration_response(_extraction([bad]), {1}, {10}, {20})
    assert got == []


def test_an_item_id_that_was_never_sent_is_dropped():
    got = comprehend.parse_integration_response(_extraction([ONE]), {2}, {10}, {20})
    assert got == []


def test_a_new_entity_and_a_new_event_are_accepted():
    """Presence sibling for the four rejection tests: a parser that returned []
    unconditionally would satisfy every one of them."""
    fresh = {
        "item_id": 1,
        "entities": [{"name": "Moldova", "type": "country", "aliases": []}],
        "events": [
            {
                "summary": "Border checks tightened",
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }
    got = comprehend.parse_integration_response(_extraction([fresh]), {1}, set(), set())
    assert got[0]["entities"][0]["name"] == "Moldova"
    assert got[0]["events"][0]["type"] == "action"
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
_ENTITY_TYPES = {"country", "institution", "company", "person", "instrument"}
_EVENT_TYPES = {"action", "statement", "disclosure"}
_COMMITMENT = {"in_force", "committed", "intended", "proposed"}
_STANDING = {"verified", "official", "reported", "attributed", "alleged"}


def parse_integration_response(
    resp: dict,
    offered_item_ids: set[int],
    offered_entity_ids: set[int],
    offered_event_ids: set[int],
) -> list[dict]:
    """Validated extractions, one per item. Drops an item WHOLE on any defect.

    Dropping the whole item rather than the bad part is deliberate: a
    half-written extraction is a claim about the world that no source made, and
    the item can be retried. A hallucinated candidate id is the specific hazard
    -- the model may name an id that was never offered, and writing it would
    attach this item's assertion to an unrelated entity or event.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("integration response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_extraction":
            rows = block.get("input", {}).get("items")
            if not isinstance(rows, list):
                raise ValueError("emit_extraction input missing 'items' list")
            return [
                p
                for r in rows
                if isinstance(r, dict)
                and (p := _validate_item(r, offered_item_ids,
                                         offered_entity_ids, offered_event_ids))
            ]
    raise ValueError("no emit_extraction tool_use block in response")


def _validate_item(row, item_ids, entity_ids, event_ids) -> dict | None:
    item_id = row.get("item_id")
    if not isinstance(item_id, int) or item_id not in item_ids:
        return None

    entities = []
    for e in row.get("entities") or []:
        if not isinstance(e, dict):
            return None
        cid = e.get("candidate_id")
        if cid is not None:
            if cid not in entity_ids:
                return None
            entities.append({"candidate_id": cid})
            continue
        name, etype = e.get("name"), e.get("type")
        if not (isinstance(name, str) and name.strip() and etype in _ENTITY_TYPES):
            return None
        aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
        entities.append({"name": name.strip(), "type": etype, "aliases": aliases})

    events = []
    for ev in row.get("events") or []:
        if not isinstance(ev, dict) or ev.get("standing") not in _STANDING:
            return None
        cid = ev.get("candidate_id")
        if cid is not None:
            if cid not in event_ids:
                return None
            events.append({"candidate_id": cid, "standing": ev["standing"]})
            continue
        summary, etype = ev.get("summary"), ev.get("type")
        commitment = ev.get("commitment_state")
        if not (isinstance(summary, str) and summary.strip()):
            return None
        if etype not in _EVENT_TYPES or commitment not in _COMMITMENT:
            return None
        events.append(
            {
                "summary": summary.strip(),
                "type": etype,
                "commitment_state": commitment,
                "standing": ev["standing"],
            }
        )

    if not entities or not events:
        return None
    return {"item_id": item_id, "entities": entities, "events": events}
```

- [ ] **Step 4: Run to verify they pass.** 6 new tests.
- [ ] **Step 5: Full gate.** Expected: **1509 passed** (1503 + 6).
- [ ] **Step 6: Commit**

Subject: `feat(comprehend): a candidate id we never offered is a hallucination, not data`

---

## Task 9: The write path and its savepoints

**Files:**
- Modify: `comprehend.py`
- Modify: `tests/test_comprehend_integration.py`

**Interfaces:**
- Consumes: `parse_integration_response`, `SurfaceIndex`, `Tally`.
- Produces: `comprehend.write_extraction(conn, extraction: dict, index: SurfaceIndex, tally: Tally) -> bool`, `comprehend.write_batch(conn, extractions: list[dict], index: SurfaceIndex, tally: Tally) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_comprehend_integration.py`:

```python
def _item(kb, title="Ukraine ceasefire", h="H1"):
    outlet_id = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Reuters', 'wire') "
        "ON CONFLICT DO NOTHING RETURNING id"
    ).fetchone()
    if outlet_id is None:
        outlet_id = kb.execute("SELECT id FROM outlets LIMIT 1").fetchone()
    oid = outlet_id[0]
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', %s, %s) RETURNING id",
        (oid, title, h),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', 'topical', 1)",
        (iid,),
    )
    return iid


def _fresh(item_id, summary="Border checks tightened", name="Moldova"):
    return {
        "item_id": item_id,
        "entities": [{"name": name, "type": "country", "aliases": []}],
        "events": [
            {
                "summary": summary,
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }


def test_writing_an_extraction_creates_the_whole_chain(kb):
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()
    index = comprehend.SurfaceIndex([])

    assert comprehend.write_extraction(kb, _fresh(iid), index, tally) is True
    kb.commit()

    assert kb.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM event_entities").fetchone()[0] == 1
    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 1
    assert kb.execute(
        "SELECT integrated_at IS NOT NULL FROM item_triage WHERE item_id = %s", (iid,)
    ).fetchone()[0] is True
    assert tally.entities_created == 1
    assert tally.events_created == 1
    assert tally.assertions_written == 1


def test_provenance_is_stamped_on_every_extracted_row(kb):
    iid = _item(kb)
    kb.commit()
    comprehend.write_extraction(kb, _fresh(iid), comprehend.SurfaceIndex([]),
                                comprehend.Tally())
    kb.commit()
    for table in ("entities", "events", "assertions"):
        row = kb.execute(
            f"SELECT extractor_model, prompt_version FROM {table}"
        ).fetchone()
        assert row[0], f"{table}.extractor_model was not stamped"
        assert row[1] == comprehend.INTEGRATE_PROMPT_VERSION


def test_matching_a_candidate_event_records_corroboration(kb):
    """Two items, two outlets, one event. This is the property claims cannot
    represent and the reason this layer exists."""
    ent = _entity(kb)
    ev = _event(kb, ent)
    other = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Guardian', 'wire') RETURNING id"
    ).fetchone()[0]
    iid = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', 'H9') RETURNING id",
        (other,),
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, 'material', 'topical', 1)",
        (iid,),
    )
    kb.commit()

    tally = comprehend.Tally()
    extraction = {
        "item_id": iid,
        "entities": [{"candidate_id": ent}],
        "events": [{"candidate_id": ev, "standing": "reported"}],
    }
    assert comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]),
                                       tally) is True
    kb.commit()

    assert kb.execute("SELECT count(*) FROM events").fetchone()[0] == 1, (
        "matching a candidate must NOT create a second event row"
    )
    assert tally.events_matched == 1
    assert tally.events_created == 0


def test_a_bad_item_does_not_take_its_neighbours_down(kb):
    """THE savepoint test, and it is two assertions rather than one.

    events.type is NOT NULL with no default, so one malformed extraction raises
    a CHECK violation. Under a batch-wide transaction that aborts the whole
    batch and loses all three items -- and an absence-only assertion would pass
    identically against that broken version. The presence half is what proves
    the savepoint exists.
    """
    good1 = _item(kb, "One", h="Ha")
    bad = _item(kb, "Two", h="Hb")
    good2 = _item(kb, "Three", h="Hc")
    kb.commit()

    batch = [
        _fresh(good1, summary="A", name="Alpha"),
        {  # bypasses the parser deliberately: an invalid enum reaching the DB
            "item_id": bad,
            "entities": [{"name": "Beta", "type": "country", "aliases": []}],
            "events": [
                {
                    "summary": "B",
                    "type": "NOT_A_TYPE",
                    "commitment_state": "in_force",
                    "standing": "reported",
                }
            ],
        },
        _fresh(good2, summary="C", name="Gamma"),
    ]
    tally = comprehend.Tally()
    written = comprehend.write_batch(kb, batch, comprehend.SurfaceIndex([]), tally)
    kb.commit()

    # Absence: the bad item wrote nothing and is not marked done.
    assert kb.execute(
        "SELECT integrated_at FROM item_triage WHERE item_id = %s", (bad,)
    ).fetchone()[0] is None
    assert not kb.execute(
        "SELECT 1 FROM assertions a JOIN items i ON i.id = a.item_id WHERE i.id = %s",
        (bad,),
    ).fetchone()
    # Presence: its neighbours survived. Without this the test passes against a
    # batch-wide transaction that lost all three.
    assert written == 2
    for good in (good1, good2):
        assert kb.execute(
            "SELECT integrated_at IS NOT NULL FROM item_triage WHERE item_id = %s",
            (good,),
        ).fetchone()[0] is True
    assert tally.items_lost_to_savepoint == 1


def test_an_instrument_entity_shadowing_a_company_is_refused(kb):
    """bqa.9 item 5, enforced in CODE and not only in the prompt.

    jx9.5 froze claim text on a MODEL-SUPPLIED field and the model simply chose
    the other value. A guard must test something the code can see for itself.
    """
    company = kb.execute(
        "INSERT INTO entities (name, type) VALUES ('Apple', 'company') RETURNING id"
    ).fetchone()[0]
    kb.execute(
        "INSERT INTO entity_instruments (entity_id, symbol, asset_class) "
        "VALUES (%s, 'AAPL', 'equity')",
        (company,),
    )
    iid = _item(kb, h="Hd")
    kb.commit()

    tally = comprehend.Tally()
    extraction = {
        "item_id": iid,
        "entities": [{"name": "AAPL", "type": "instrument", "aliases": []}],
        "events": [
            {
                "summary": "Shares moved",
                "type": "action",
                "commitment_state": "in_force",
                "standing": "reported",
            }
        ],
    }
    comprehend.write_extraction(kb, extraction, comprehend.SurfaceIndex([]), tally)
    kb.commit()

    assert kb.execute(
        "SELECT count(*) FROM entities WHERE type = 'instrument'"
    ).fetchone()[0] == 0
    assert tally.instrument_entity_refused == 1


def test_an_event_this_pipeline_CREATED_can_be_retrieved_as_a_candidate(kb):
    """The round-trip, and it is the most important test in this file.

    Every other test here builds its candidate events with a fixture that sets
    occurred_at explicitly. Production does not: it writes what
    write_extraction writes. An earlier draft of this plan omitted occurred_at
    from the INSERT, so every created event had NULL, candidate_events'
    `occurred_at >= now() - interval` excluded it, events_matched could never
    leave 0, and the corroboration floor failed BY CONSTRUCTION -- with the
    whole suite green, because the fixtures built rows production cannot.

    That is tdd-plan-fixtures-drift-from-contracts exactly. Write the row with
    the real function, then read it back with the real query.
    """
    iid = _item(kb)
    kb.commit()
    tally = comprehend.Tally()
    index = comprehend.SurfaceIndex([])
    assert comprehend.write_extraction(kb, _fresh(iid), index, tally) is True
    kb.commit()

    entity_id = kb.execute("SELECT id FROM entities").fetchone()[0]
    candidates = comprehend.candidate_events(kb, [entity_id], comprehend.Tally())
    assert len(candidates) == 1, (
        "an event this pipeline just created must be offerable as a candidate, "
        "or corroboration is impossible no matter how well the matcher works"
    )
    assert candidates[0]["summary"] == "Border checks tightened"


def test_reprocessing_the_same_item_does_not_duplicate_assertions(kb):
    iid = _item(kb)
    kb.commit()
    for _ in range(2):
        comprehend.write_extraction(kb, _fresh(iid), comprehend.SurfaceIndex([]),
                                    comprehend.Tally())
        kb.commit()
    assert kb.execute("SELECT count(*) FROM assertions").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

Add to `comprehend.py`:

```python
def _resolve_entity(conn, spec: dict, tally: Tally) -> int | None:
    """A candidate id, or an upserted new entity. None means refused."""
    if "candidate_id" in spec:
        tally.entities_resolved += 1
        return spec["candidate_id"]

    name, etype = spec["name"], spec["type"]
    # bqa.9 item 5, enforced where the code can see it. The prompt states the
    # rule too, but jx9.5 showed a guard on a model-supplied field gets walked
    # around by the model choosing the other value.
    if etype == "instrument":
        shadowed = conn.execute(
            "SELECT 1 FROM entity_instruments ei JOIN entities e ON e.id = ei.entity_id "
            "WHERE lower(ei.symbol) = lower(%s) AND e.type = 'company' LIMIT 1",
            (name,),
        ).fetchone()
        if shadowed:
            tally.instrument_entity_refused += 1
            log.warning(
                f"Comprehend: refused instrument entity {name!r}; a company "
                f"already maps that symbol (one entity per company)"
            )
            return None

    row = conn.execute(
        "INSERT INTO entities (name, type, aliases, extractor_model, prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (lower(name), type) DO NOTHING RETURNING id",
        (name, etype, spec.get("aliases") or [], _integrate_model(),
         INTEGRATE_PROMPT_VERSION),
    ).fetchone()
    if row:
        tally.entities_created += 1
        return row[0]
    tally.entities_resolved += 1
    return conn.execute(
        "SELECT id FROM entities WHERE lower(name) = lower(%s) AND type = %s",
        (name, etype),
    ).fetchone()[0]


def write_extraction(conn, extraction: dict, index: SurfaceIndex, tally: Tally) -> bool:
    """Write one item's extraction inside its OWN savepoint.

    capture.store_items already established this pattern and documented why:
    each entry gets its own savepoint so neither a duplicate nor a rejected
    entry can lose the entries around it. Here the payload is far more
    expensive to re-derive, so the argument is stronger, not weaker.

    Returns True if the item was integrated.
    """
    item_id = extraction["item_id"]
    try:
        with conn.transaction():
            entity_ids = []
            for spec in extraction["entities"]:
                eid = _resolve_entity(conn, spec, tally)
                if eid is not None:
                    entity_ids.append(eid)
                    if "name" in spec:
                        index.add_entity(eid, spec["name"], spec.get("aliases") or [])
            if not entity_ids:
                raise ValueError("no entity survived resolution")

            for ev in extraction["events"]:
                if "candidate_id" in ev:
                    event_id = ev["candidate_id"]
                    tally.events_matched += 1
                else:
                    # occurred_at is NOT optional here, and omitting it is fatal
                    # rather than untidy. candidate_events filters
                    # `occurred_at >= now() - interval`, which is FALSE for
                    # NULL -- so an event created without one can never be
                    # offered as a candidate, events_matched stays 0, and the
                    # corroboration floor fails BY CONSTRUCTION while the
                    # matcher is working perfectly.
                    #
                    # It is taken from the item's published_at, an observed fact
                    # capture already stores, rather than extracted: a model
                    # guess here would be one more unmeasured field, and article
                    # publication is a good enough proxy for a 14-day window.
                    event_id = conn.execute(
                        "INSERT INTO events (summary, type, commitment_state, "
                        "  occurred_at, extractor_model, prompt_version) "
                        "VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s) RETURNING id",
                        (ev["summary"], ev["type"], ev["commitment_state"],
                         extraction.get("published_at"),
                         _integrate_model(), INTEGRATE_PROMPT_VERSION),
                    ).fetchone()[0]
                    tally.events_created += 1

                for eid in entity_ids:
                    conn.execute(
                        "INSERT INTO event_entities (event_id, entity_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (event_id, eid),
                    )
                # source_relationship is deliberately NOT written: no rubric
                # exists for it, and bqa.8 owns its fate.
                written = conn.execute(
                    "INSERT INTO assertions (item_id, event_id, standing, "
                    "  extractor_model, prompt_version) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (item_id, event_id) DO NOTHING RETURNING id",
                    (item_id, event_id, ev["standing"], _integrate_model(),
                     INTEGRATE_PROMPT_VERSION),
                ).fetchone()
                if written:
                    tally.assertions_written += 1

            conn.execute(
                "UPDATE item_triage SET integrated_at = now(), "
                "  integrate_prompt_version = %s "
                "WHERE item_id = %s AND verdict = 'material'",
                (INTEGRATE_PROMPT_VERSION, item_id),
            )
    except Exception:
        tally.items_lost_to_savepoint += 1
        tally.failed_integration += 1
        log.warning(f"Comprehend: item {item_id} rolled back its savepoint",
                    exc_info=True)
        conn.execute(
            "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
            "WHERE item_id = %s",
            (item_id,),
        )
        return False
    return True


def write_batch(conn, extractions, index: SurfaceIndex, tally: Tally) -> int:
    """One transaction for the batch, one savepoint per item inside it.

    The OUTER `conn.transaction()` is load-bearing and must not be removed as
    redundant. db.connect() sets autocommit=False, so psycopg's
    `conn.transaction()` is a real transaction when it is the outermost block
    and a SAVEPOINT only when one is already open. Without this wrapper the
    behaviour of write_extraction depends on whether the caller happens to have
    an open transaction -- which differs between a test that just committed and
    the run loop, which has an open SELECT. A test would then assert semantics
    production never uses.
    """
    with conn.transaction():
        return sum(1 for e in extractions if write_extraction(conn, e, index, tally))
```

- [ ] **Step 4: Run to verify they pass.** 6 new tests.
- [ ] **Step 5: Full gate.** Expected: **1516 passed** (1509 + 7).
- [ ] **Step 6: Commit**

Subject: `feat(comprehend): a malformed extraction costs one item, not its batch`

---

## Task 10: Wire the run loop

**Files:**
- Modify: `comprehend.py` (replace the `run` body from Task 2)
- Modify: `tests/test_comprehend_triage.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a complete `comprehend.run(conn) -> Tally`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_comprehend_triage.py`:

```python
def test_a_full_pass_triages_and_integrates_with_both_calls_stubbed(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    tracked = _add_item(kb, "Ukraine signals a ceasefire", h="Ht")
    topical = _add_item(kb, "Sahel coup attempt reported", h="Hp")
    kb.commit()

    def fake_triage(_req):
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_triage",
                    "input": {"items": [{"id": topical, "material": True}]},
                }
            ],
        }

    def fake_integrate(req):
        sent = [
            int(line.split("item_id=")[1].split()[0])
            for line in req["messages"][0]["content"].splitlines()
            if "item_id=" in line
        ]
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_extraction",
                    "input": {
                        "items": [
                            {
                                "item_id": i,
                                "entities": [
                                    {"name": f"E{i}", "type": "country", "aliases": []}
                                ],
                                "events": [
                                    {
                                        "summary": f"S{i}",
                                        "type": "action",
                                        "commitment_state": "in_force",
                                        "standing": "reported",
                                    }
                                ],
                            }
                            for i in sent
                        ]
                    },
                }
            ],
        }

    monkeypatch.setattr(comprehend, "call_triage", fake_triage)
    monkeypatch.setattr(comprehend, "call_integration", fake_integrate)

    tally = comprehend.run(kb)
    kb.commit()

    assert tally.triaged_by_rules == 1, "Ukraine matched the tracked half, no model"
    assert tally.triaged_by_model == 1, "Sahel needed the model half"
    assert tally.material == 2
    assert tally.assertions_written == 2
    reasons = dict(
        kb.execute("SELECT item_id, reason FROM item_triage").fetchall()
    )
    assert reasons[tracked] == "tracked_entity"
    assert reasons[topical] == "topical"
```

- [ ] **Step 2: Run to verify it fails.** Expected: `no attribute 'call_triage'`.

- [ ] **Step 3: Implement**

Add the two seams and replace `run` in `comprehend.py`:

```python
def call_triage(request: dict) -> dict:
    """The network seam for triage. Tests monkeypatch this.

    Reuses brief._post_messages rather than adding a second HTTP path: it
    already carries the retry and the generous timeout that a synchronous
    tool-use generation needs.
    """
    import brief

    return brief._post_messages(request)


def call_integration(request: dict) -> dict:
    """The network seam for integration. Tests monkeypatch this."""
    import brief

    return brief._post_messages(request)


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run(conn) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS.

    Commit boundaries are load-bearing: one transaction per micro-batch, one
    savepoint per item inside it (spec section 6.4).
    """
    tally = Tally(enabled=bool(common.COMPREHEND_ENABLED))
    if not tally.enabled:
        log.info("Comprehend: disabled by COMPREHEND_ENABLED; nothing read")
        return tally

    deadline = time.monotonic() + DEADLINE_SECONDS
    index = SurfaceIndex.build(conn)
    outlets = dict(conn.execute("SELECT id, name FROM outlets").fetchall())

    pending = pending_triage(conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS))
    tally.items_seen = len(pending)

    # --- Triage: rules half first, so the model never sees what a lookup answered.
    undecided = []
    for item in pending:
        hit = triage_by_rules(item, index)
        if hit:
            record_triage(conn, item["id"], "material", hit.reason, None,
                          TRIAGE_PROMPT_VERSION)
            tally.triaged_by_rules += 1
            tally.material += 1
        else:
            undecided.append(item)
    conn.commit()

    # --- Triage: model half, on the remainder only.
    for batch in _chunk(undecided, int(common.COMPREHEND_TRIAGE_BATCH)):
        if time.monotonic() >= deadline:
            break
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        try:
            verdicts = parse_triage_response(
                call_triage(build_triage_request(payload)), {it["id"] for it in batch}
            )
        except Exception:
            tally.failed_triage += len(batch)
            log.warning("Comprehend: triage batch failed", exc_info=True)
            for it in batch:
                record_triage(conn, it["id"], "failed", "error", _triage_model(),
                              TRIAGE_PROMPT_VERSION)
            conn.commit()
            continue
        for it in batch:
            material = verdicts.get(it["id"], False)
            record_triage(
                conn, it["id"],
                "material" if material else "immaterial",
                "topical" if material else "none",
                _triage_model(), TRIAGE_PROMPT_VERSION,
            )
            tally.triaged_by_model += 1
            tally.material += int(material)
            tally.immaterial += int(not material)
        conn.commit()

    # --- The unconfounded control arm.
    for item_id in select_sampled(conn, TRIAGE_PROMPT_VERSION,
                                  int(common.COMPREHEND_SAMPLE_PER_DAY)):
        record_triage(conn, item_id, "material", "sampled", None, TRIAGE_PROMPT_VERSION)
        tally.sampled += 1
    conn.commit()

    # --- Integration.
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id "
        "WHERE t.verdict = 'material' AND t.integrate_attempts < 3 "
        "  AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s) "
        "ORDER BY i.id LIMIT %s",
        (INTEGRATE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS)),
    ).fetchall()
    # published_at is carried because write_extraction needs it for
    # events.occurred_at. Without it every created event is invisible to
    # candidate_events and corroboration is impossible.
    material_items = [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
        }
        for r in rows
    ]
    published = {it["id"]: it["published_at"] for it in material_items}

    for batch in _chunk(material_items, int(common.COMPREHEND_INTEGRATE_BATCH)):
        if time.monotonic() >= deadline:
            break
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        hits = [
            sf
            for it in batch
            for sf in index.match(f"{it['title']}\n{it['body']}")
            if sf.entity_id is not None
        ]
        entity_ids = list(dict.fromkeys(sf.entity_id for sf in hits))[
            :CANDIDATE_ENTITY_CAP
        ]
        cand_entities = [
            {"id": r[0], "name": r[1], "type": r[2]}
            for r in conn.execute(
                "SELECT id, name, type FROM entities WHERE id = ANY(%s)",
                (entity_ids,),
            ).fetchall()
        ] if entity_ids else []
        cand_events = candidate_events(conn, entity_ids, tally)

        try:
            extractions = parse_integration_response(
                call_integration(
                    build_integration_request(payload, cand_entities, cand_events)
                ),
                {it["id"] for it in batch},
                {c["id"] for c in cand_entities},
                {c["id"] for c in cand_events},
            )
        except Exception:
            tally.failed_integration += len(batch)
            log.warning("Comprehend: integration batch failed", exc_info=True)
            conn.execute(
                "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
                "WHERE item_id = ANY(%s)",
                ([it["id"] for it in batch],),
            )
            conn.commit()
            continue

        for e in extractions:
            e["published_at"] = published.get(e["item_id"])
        write_batch(conn, extractions, index, tally)
        conn.commit()

    tally.gave_up_integration = conn.execute(
        "SELECT count(*) FROM item_triage WHERE integrate_attempts >= 3"
    ).fetchone()[0]
    tally.gave_up_triage = conn.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'failed' AND attempts >= 3"
    ).fetchone()[0]
    log.info(f"Comprehend: {tally}")
    return tally
```

`brief._post_messages(payload) -> dict` is verified to exist at `brief.py:2861`. It is a raw Messages API call with one retry, so there is no second HTTP path to add.

**One thing to watch, and it has bitten this repo before.** `_post_messages` uses `SIGNALS_TIMEOUT` and `SIGNALS_MAX_ATTEMPTS` — sized for the signals extraction. The integration call runs at `max_tokens=8192` against a larger prompt, so it is a materially slower request. `signals-extraction-separate-call-followup` records the exact failure: a 30s timeout copied from a Haiku call was too short for Sonnet, the call timed out, and the day's signals were wiped. If integration calls start failing on read timeouts, that is the cause — raise the timeout for this path rather than shortening the prompt.

- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Full gate.** Expected: **1517 passed** (1516 + 1).
- [ ] **Step 6: Commit**

Subject: `feat(comprehend): the pass runs end to end, still reading nothing back`

---

## Task 11: The pre-registered gate

**Files:**
- Create: `scripts/score_comprehension.py`

**Interfaces:**
- Consumes: the tables written above.
- Produces: a read-only CLI printing distributions and PASS/FAIL against the thresholds.

This script **must land before the flag is turned on.** Measured afterwards, its thresholds are post-hoc rationalisation wearing a gate's clothes.

- [ ] **Step 1: Write the script**

Create `scripts/score_comprehension.py`:

```python
"""Score the comprehension pipeline against its PRE-REGISTERED gate.

Spec section 8. Read-only: it writes nothing and is safe against production.

Run:  py scripts/score_comprehension.py
"""

import sys

import db

# Pre-registered 2026-09-04, BEFORE the first real run. Do not tune these to
# make a run pass -- a threshold moved after seeing the data measures nothing.
MIN_DISTINCT_AT_10PCT = 2
MAX_SINGLE_VALUE_SHARE = 0.90
CORROBORATION_FLOOR = 0.10
CORROBORATION_CEILING = 0.60

ENUMS = [
    ("events", "type"),
    ("events", "commitment_state"),
    ("assertions", "standing"),
]


def distribution(conn, table, column, reason=None):
    if reason:
        rows = conn.execute(
            f"SELECT a.{column}, count(*) FROM {table} a "
            "JOIN item_triage t ON t.item_id = a.item_id "
            f"WHERE a.{column} IS NOT NULL AND t.reason = %s GROUP BY 1",
            (reason,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {column}, count(*) FROM {table} "
            f"WHERE {column} IS NOT NULL GROUP BY 1"
        ).fetchall()
    total = sum(c for _, c in rows)
    return {v: c / total for v, c in rows} if total else {}, total


def score_enum(shares, total):
    if total == 0:
        return False, "no rows"
    at_10 = sum(1 for s in shares.values() if s >= 0.10)
    top = max(shares.values())
    ok = at_10 >= MIN_DISTINCT_AT_10PCT and top <= MAX_SINGLE_VALUE_SHARE
    detail = ", ".join(f"{v}={s:.0%}" for v, s in sorted(shares.items(), key=lambda x: -x[1]))
    return ok, f"n={total} {detail} (>=10%: {at_10}, top {top:.0%})"


def by_depth_tier(conn, table, column):
    """Enum distribution split by the source item's body length.

    Spec 12.3 amendment 1. Measured 2026-09-05: 15 of 24 outlets have a median
    body under 150 characters, and 41% of captured volume is Google News proxy
    items whose body is the headline restated. An aggregate distribution over
    that corpus substantially measures feed composition rather than model
    judgement, so the tiers are reported alongside it.
    """
    if table == "events":
        join = (
            "FROM events e "
            "JOIN assertions a ON a.event_id = e.id "
            "JOIN items i ON i.id = a.item_id"
        )
        col = f"e.{column}"
    else:
        join = "FROM assertions a JOIN items i ON i.id = a.item_id"
        col = f"a.{column}"
    return conn.execute(
        "SELECT CASE WHEN coalesce(length(i.body), 0) < 150 THEN '<150' "
        "            WHEN length(i.body) < 350 THEN '150-350' "
        "            ELSE '350+' END AS tier, "
        f"       {col} AS value, count(*) AS n "
        f"{join} "
        f"WHERE {col} IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    ).fetchall()


def corroboration(conn):
    row = conn.execute(
        "WITH per_event AS ("
        "  SELECT a.event_id, count(DISTINCT i.outlet_id) AS outlets "
        "  FROM assertions a JOIN items i ON i.id = a.item_id "
        "  GROUP BY a.event_id) "
        "SELECT count(*), count(*) FILTER (WHERE outlets >= 2) FROM per_event"
    ).fetchone()
    total, multi = row
    return (multi / total if total else 0.0), total, multi


def main() -> int:
    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    failures = []
    with db.connect() as conn:
        print("=== Enum variance (spec 8.1) ===")
        for table, column in ENUMS:
            shares, total = distribution(conn, table, column)
            ok, detail = score_enum(shares, total)
            print(f"{'PASS' if ok else 'FAIL'}  {table}.{column}: {detail}")
            if not ok:
                failures.append(f"{table}.{column}")

        print("\n=== Per-arm, with `sampled` as the unconfounded control (8.1) ===")
        print("A topical arm that varies while sampled does not means the")
        print("extractor is being flattered by its sibling's selection.")
        for reason in ("tracked_entity", "topical", "sampled"):
            shares, total = distribution(conn, "assertions", "standing", reason)
            ok, detail = score_enum(shares, total)
            print(f"  standing[{reason}]: {detail}")

        print("\n=== Corroboration, BOTH directions (spec 8.2) ===")
        rate, total, multi = corroboration(conn)
        print(f"events={total} multi-outlet={multi} rate={rate:.1%}")
        if total == 0:
            failures.append("corroboration: no events")
            print("FAIL  no events to measure")
        elif rate < CORROBORATION_FLOOR:
            failures.append("corroboration below floor")
            print(f"FAIL  below the {CORROBORATION_FLOOR:.0%} floor: the event "
                  f"layer bought nothing over the claim ledger")
        elif rate > CORROBORATION_CEILING:
            failures.append("corroboration above ceiling")
            print(f"FAIL  above the {CORROBORATION_CEILING:.0%} ceiling: suspect "
                  f"the matcher is OVER-MERGING distinct events")
        else:
            print("PASS")

        created, matched = conn.execute(
            "SELECT count(*) FILTER (WHERE prompt_version IS NOT NULL), count(*) "
            "FROM events"
        ).fetchone()
        print(f"\nmechanism check: events rows={matched}, extractor-stamped={created}")
        print("If corroboration disagrees with the match rate, trust the")
        print("disagreement -- a secondary signal contradicting the headline is")
        print("the tell that the probe measured the wrong layer.")

        # Spec 12.3 amendment 1. Measured 2026-09-05: 15 of 24 outlets have a
        # median body under 150 chars and 41% of volume is Google News proxy
        # items whose body is the headline restated. An aggregate enum
        # distribution therefore substantially measures FEED COMPOSITION.
        print("\n=== Enum variance by body-depth tier (spec 12.3) ===")
        for table, column in ENUMS:
            print(f"  {table}.{column}:")
            for tier, value, n in by_depth_tier(conn, table, column):
                print(f"    [{tier}] {value}: {n}")

        # Spec 12.3 amendment 2. standing is the field at risk, and its likely
        # failure is NOT low variance -- it is becoming a function of the
        # OUTLET rather than of the item, which looks healthy in aggregate
        # while carrying no per-item information. No aggregate distribution
        # can show that; this can.
        print("\n=== standing variance WITHIN each outlet (spec 12.3) ===")
        print("A field constant within every outlet is degenerate however")
        print("varied it looks overall -- severity's failure in disguise.")
        rows = conn.execute(
            "SELECT o.name, count(DISTINCT a.standing) AS distinct_standing, "
            "       count(*) AS n "
            "FROM assertions a "
            "JOIN items i ON i.id = a.item_id "
            "JOIN outlets o ON o.id = i.outlet_id "
            "GROUP BY o.name HAVING count(*) >= 10 ORDER BY 2, 3 DESC"
        ).fetchall()
        for name, distinct, n in rows:
            flag = "  <-- CONSTANT" if distinct <= 1 else ""
            print(f"  {name}: {distinct} distinct over n={n}{flag}")
        constant = [r[0] for r in rows if r[1] <= 1]
        if rows and len(constant) == len(rows):
            failures.append("standing is constant within every outlet")
            print("FAIL  standing carries no per-item information")

        print("\n=== Triage reason distribution (spec 5.1.1) ===")
        print("Interpretable only AFTER entities has accumulated. tracked_story")
        print("is expected at ZERO: nothing in production writes `stories`.")
        for reason, n in conn.execute(
            "SELECT reason, count(*) FROM item_triage GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {reason}: {n}")

    print("\n" + ("GATE FAILED: " + ", ".join(failures) if failures else "GATE PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify it runs against an empty database and fails honestly**

Run: `py scripts/score_comprehension.py`
Expected: exit 1, "no rows" for each enum and "no events to measure". An empty KB must FAIL the gate, not pass it vacuously — that is the whole shape this repo keeps getting wrong.

- [ ] **Step 3: Full gate.** Expected: **1517 passed** (unchanged; the script has no tests of its own because it is a read-only reporting tool whose logic is thresholds).

- [ ] **Step 4: Commit**

Subject: `feat(comprehend): the gate is pre-registered, and an empty KB fails it`

---

## Before enabling in production

1. Answer spec §10 items 3 and 4 on the host (body depth, real volume) and reconcile `COMPREHEND_MAX_ITEMS` and the batch sizes against the answers.
2. Turn it on with a **settings row**, not an environment change — `import_settings_from_env` runs only while `settings` is empty:
   ```sql
   INSERT INTO settings (key, user_id, value) VALUES ('COMPREHEND_ENABLED', NULL, 'true')
   ON CONFLICT (key) WHERE user_id IS NULL DO UPDATE SET value='true', updated_at=now();
   ```
3. **Verify by effect, not by reading the row back.** A bool knob logs no warning for a bad value: `'ture'` and `''` both silently mean false. The effect to check is `item_triage` gaining rows.
4. Run `py scripts/score_comprehension.py` after a week and record the result whether it passes or fails.

---

## Self-Review

**Spec coverage.** §1 scope → Tasks 7-9 (no claims, no links, no stories, `source_relationship` absent from the tool schema in Task 7). §2.2 materiality → Tasks 4-6. §2.3 entity resolution → Tasks 7-9. §4 migration → Task 1. §5.1 matcher → Task 3. §5.1.1 cold start → surfaced in Task 11's reason distribution. §5.2 model half → Task 5. §5.3 sampled arm → Task 6. §6.1 no enum leakage → Task 7, two tests. §6.3 one-entity-per-company → Task 9. §6.4 savepoints → Task 9. §7.1 ships off → Task 2. §7.2 knobs and packaging → Task 2. §7.2.1 cadence → Task 2. §7.3 bounds → Tasks 2 and 10. §7.4 failure modes → Tasks 5, 8, 9, 10. §7.5 tally → Task 2, populated throughout. §8 gate → Task 11. §9 testing → every task.

**Gap found and closed:** §7.4's "candidate id not in the offered set" needed its own test rather than being folded into parsing; it is now two tests in Task 8 (entity and event separately), because one covers only one code path.

**Placeholder scan:** none. Every code step carries real code; every test step carries real assertions.

**Type consistency:** `Tally` field names in Task 2 match every increment in Tasks 4-10. `SurfaceForm.reason` values match the `item_triage.reason` CHECK in Task 1. `parse_integration_response`'s output shape matches `write_extraction`'s input. `record_triage`'s argument order is identical at all seven call sites.

**Remaining assumption the first implementer should verify:** `outlets` is read into a dict in Task 10 via `SELECT id, name FROM outlets`. At 26 feeds this is trivially small, but if the outlet table has grown well beyond the feed count on the host, scope that query rather than loading it whole.

---

## Defects found by red-team review, and fixed

Recorded because most of them would have produced a **green suite and a dead pipeline**, which is this plan's own stated worst case.

| Defect | Why it mattered |
|---|---|
| **`events.occurred_at` was never written** | Nullable, so every created event had NULL. `candidate_events` filters `occurred_at >= now() - interval`, false for NULL — so no self-created event could ever be a candidate, `events_matched` was pinned at 0, and the corroboration floor failed **by construction**. Every test passed, because the fixtures set `occurred_at` explicitly: rows production cannot make. Textbook `tdd-plan-fixtures-drift-from-contracts`. Now taken from `items.published_at`, with a round-trip test that writes with the real function and reads back with the real query. |
| **`write_batch` opened no transaction** | `db.connect()` is `autocommit=False`, so `conn.transaction()` is a real transaction when outermost and a SAVEPOINT only when one is open. The test committed first, so it exercised the transaction path; the run loop has an open SELECT, so production takes the savepoint path. The savepoint test asserted semantics production never uses. |
| **`select_sampled` was per-run, not per-day** | Hourly schedule × 20 = 480/day in the expensive tier, not 20. A ~400% overhead where §5.3 costed ~17% — the same order of cost error as the proposal this control arm was chosen over. |
| **`_outlet` always INSERTed** | `outlets` is `UNIQUE (lower(name))`; every test adding a second item raised `UniqueViolation`. Now get-or-create. |
| **`claims` fixture omitted `first_seen`** | `DATE NOT NULL`, no default — `NotNullViolation`. |
| **Mid-file `import re` and `import pytest as _pytest`** | ruff E402 is active (no ruff config in this repo); both fail `ruff check .` with exit 1. Verified with a live probe. This trap is already written down in `brief-local-run`, and the plan walked into it anyway. |
| **Knob keys were prefixed** | Convention is key = attribute, `env=` carries the prefix (`common.py:183`, `:249`). The plan would have produced `common.NEWSBRIEF_TRIAGE_MODEL`. |
| **"six knobs" while listing seven** | Counting error, corrected throughout. |

**One review claim was wrong:** the report stated `HEAD` was `9264278`. That was HEAD at session start; the spec and six other commits have landed since. The underlying advice — verify the baseline rather than trusting a written number — is right regardless, and is now in Global Constraints.

**One review claim was half right:** `first_seen` is `NOT NULL`, but `last_reaffirmed` is nullable (`0006:194`). The fixture supplies both anyway, for the different reason that `claim_store._row_to_claim` treats a NULL `last_reaffirmed` as a hard error — the column's nullability and the loader's contract disagree.
