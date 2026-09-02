# Claim Ledger Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `brief_memory`'s JSON claim ledger into the `claims` table, changing no business logic, so the knowledge base becomes non-empty and Epic 4 is unblocked.

**Architecture:** A new `claim_store.py` owns rows, marshalling and id allocation, and nothing else. `merge_ledger` and `select_working_set` run unmodified on the same dicts they take today. Retirement stops being deletion and becomes a `retired_on DATE NULL` column written by the store when the merge drops a row.

**Tech Stack:** Python 3.14, psycopg 3, PostgreSQL 18, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md`

## Global Constraints

- **Pre-push gate is three commands, not one:** `ruff check .`, `ruff format --check .`, `pytest -q`. `ruff format` edits in place — `git add` every file it touches or CI fails on the committed tree while your working tree looks clean.
- **`pytest` alone reports green with the entire database layer skipped.** Every task here is DB-backed. Export a connection before running anything:
  ```bash
  docker run --rm -d --name nb_bqa10 -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief \
    -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
  export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:55432/newsbrief_test"
  ```
  Port **55432**, not 5432 — another session may hold 5432. Check with `docker ps` before binding.
- **A skip is not a pass.** Every task states an absolute expected test count. Confirm the baseline before you start; a mismatch is a stop, not a rounding error.
- **Baseline at plan time: `pytest --collect-only -q` reports 1313 tests.**
- **Never `docker compose up -d` locally** — it starts a second Telegram `getUpdates` consumer and 409s the live bot. `docker compose config` only.
- **Commit from the Bash tool, not PowerShell** — PowerShell prepends a UTF-8 BOM to the subject line.
- **Do not push.** This repo's profile is conservative; report status and stop.
- `claim_store.py` is a **new top-level module** and needs three allowlist edits (Task 2, Steps 5-7) or CI passes on a full checkout and production raises `ModuleNotFoundError`.
- **Every import belongs at the top of its file.** Steps below say "append to `tests/test_claim_store.py`"; that means append the *code* and move any new `import` line up into the existing import block at the top. Ruff's default rule set has **E402 enabled** (verified against this repo's config, which sets no `select`), so a mid-file import fails `ruff check` and the gate runs it. The imports the appended blocks introduce are `claim_store` (Task 2) and `json` (Task 5).

## File Structure

| File | Responsibility |
|---|---|
| `migrations/0007_claim_retirement_up.sql` (create) | `retired_on` column; recreate `claims_open_resolution`; add `claims_live` |
| `migrations/0007_claim_retirement_down.sql` (create) | Reverse all three, in dependency order |
| `claim_store.py` (create) | The only module that knows SQL or that `broke_on` is `resolved_on`. Rows, marshalling, id allocation, the legacy import. No business logic. |
| `brief_memory.py` (modify) | Loses `load_ledger`/`save_ledger`; gains a `next_num` parameter on `merge_ledger` and `reconcile_ledger`. Everything else untouched. |
| `brief.py` (modify) | Two call sites restructured to hold `before`; degraded-render notice |
| `supervisor.py` (modify) | One line, joining the four existing importers |
| `tests/test_claim_store.py` (create) | All store behaviour, DB-gated |
| `tests/test_brief_memory.py` (modify) | Three file-I/O tests removed; they move to `test_claim_store.py` |
| `Dockerfile`, `.github/workflows/docker-publish.yml` (modify) | Allowlist the new module |

### One deviation from the spec, and why

Spec §3 says `brief_memory.load_ledger`/`save_ledger` "become thin calls into" the store. Doing that literally creates an **import cycle**: `brief_memory` → `claim_store` → `brief_memory` (the store needs `_SEVERITY_RANK` for its `ORDER BY`).

Resolution: `brief.py` imports the store **directly**, and `brief_memory` loses both functions. `brief_memory` keeps zero database knowledge, which is what the spec's boundary actually wanted. The store hardcodes the severity ordering with a test asserting it matches `_SEVERITY_RANK`, so the duplication cannot drift silently.

---

### Task 1: Migration 0007 — retirement without deletion

**Files:**
- Create: `migrations/0007_claim_retirement_up.sql`
- Create: `migrations/0007_claim_retirement_down.sql`
- Test: `tests/test_claim_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `claims.retired_on DATE NULL`; index `claims_open_resolution` re-created with `AND retired_on IS NULL`; index `claims_live` on `(ledger_id) WHERE retired_on IS NULL`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claim_store.py`:

```python
"""Storage tests for the claim ledger cutover (news-brief-bqa.10).

DB-gated for the same reason tests/test_kb_schema.py is: a skip is not a pass,
and every assertion here is about what Postgres actually does.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    """A connection to a schema-less database: every test starts from nothing."""
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    """A fully migrated database, through 0007."""
    db.run_migrations(conn)
    conn.commit()
    return conn


def _indexdef(conn, name):
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,)
    ).fetchone()
    return row[0] if row else None


def test_claims_has_a_nullable_retired_on_date(store):
    row = store.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert row == ("date", "YES")


def test_rule_four_index_excludes_retired_claims(store):
    """0006:215-218 records the earlier version of this bug: a claim that should
    have left the index never did, and rule 4 re-fired on it every morning."""
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")


def test_a_live_index_supports_the_daily_load(store):
    assert "retired_on IS NULL" in _indexdef(store, "claims_live")


def test_0007_rolls_back_and_reapplies(store):
    """No down migration is trusted until it has been run. The index must come
    BACK on rollback, not merely be dropped -- 0006 owns it, so leaving it
    missing would corrupt the schema 0006 promises."""
    db.run_migrations(store, direction="down", steps=1)
    store.commit()
    cols = store.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'claims' AND column_name = 'retired_on'"
    ).fetchone()
    assert cols is None
    restored = _indexdef(store, "claims_open_resolution")
    assert restored is not None and "retired_on" not in restored
    db.run_migrations(store)
    store.commit()
    assert "retired_on IS NULL" in _indexdef(store, "claims_open_resolution")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: 4 failed — `retired_on` does not exist and `claims_open_resolution` has no such predicate. If they *skip*, `DATABASE_URL` is not exported; fix that before continuing, because a skip proves nothing.

- [ ] **Step 3: Write the up migration**

Create `migrations/0007_claim_retirement_up.sql`:

```sql
-- Retirement without deletion (news-brief-bqa.10).
--
-- brief_memory's TTL drops a stale claim from the ledger dict it returns.
-- Before the cutover that meant a row vanished from a JSON file; in Postgres it
-- would mean DELETE, and the three FKs pointing at claims disagree about what
-- that means: CASCADE on claim_evidence (0006:223) and thesis_claims (0006:306)
-- silently destroys evidence and membership, while story_members (0006:339) is
-- RESTRICT and raises. One sweep, two silent destructions and a hard error,
-- depending only on what the claim happens to be attached to.
--
-- Stamping a date instead keeps every referencing row intact AND records the
-- retirement decision the old TTL made and threw away, which is what keeps the
-- cutover reversible.
ALTER TABLE claims ADD COLUMN retired_on DATE NULL;

-- Rule 4 must not re-fire on retired claims. 0006:215-218 records the earlier
-- version of exactly this bug.
--
-- WARNING for news-brief-bqa.5: a partial index restricts what is INDEXED, it
-- does NOT filter a query. If rule 4's query omits `retired_on IS NULL`,
-- Postgres cannot use this index, falls back to a sequential scan, and re-fires
-- on every retired row -- the original bug, now slower. The predicate is an
-- optimisation here and an obligation there.
DROP INDEX claims_open_resolution;
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged') AND retired_on IS NULL;

-- claim_store.load_ledger runs this predicate every brief.
CREATE INDEX claims_live ON claims (ledger_id) WHERE retired_on IS NULL;
```

- [ ] **Step 4: Write the down migration**

Create `migrations/0007_claim_retirement_down.sql`:

```sql
-- Restores 0006's index rather than merely dropping ours: 0006 owns
-- claims_open_resolution, so rolling 0007 back must leave the schema 0006
-- promises, not a schema missing an index it declared.
DROP INDEX IF EXISTS claims_live;
DROP INDEX IF EXISTS claims_open_resolution;
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged');
ALTER TABLE claims DROP COLUMN IF EXISTS retired_on;
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: **4 passed.**

- [ ] **Step 6: Run the full gate**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1317 passed** (1313 + 4), 0 skipped, 0 xfailed.

- [ ] **Step 7: Commit**

```bash
git add migrations/0007_claim_retirement_up.sql migrations/0007_claim_retirement_down.sql tests/test_claim_store.py
git commit -m "feat(kb): a claim retires by taking a date, not by being deleted"
```

---

### Task 2: `claim_store.load_ledger` — the read path

**Files:**
- Create: `claim_store.py`
- Modify: `Dockerfile:35`, `.github/workflows/docker-publish.yml:7-18,71-72`
- Test: `tests/test_claim_store.py`

**Interfaces:**
- Consumes: Task 1's `retired_on`.
- Produces: `claim_store.load_ledger(conn) -> dict` returning `{"version": 1, "claims": [...]}`; `claim_store._KEY_TO_COLUMN: dict[str, str]`; `claim_store._DATE_KEYS: tuple[str, ...]`; `claim_store._SEVERITY_ORDER_SQL: str`.

- [ ] **Step 1: Write the failing tests**

Add `import brief_memory` and `import claim_store` to the import block at the **top** of
`tests/test_claim_store.py` (Task 1 deliberately left them out — an import a file does not yet
use is `F401`, and Task 1's gate would have failed on it). Then append:

```python
def _insert(conn, **overrides):
    """Insert one claim row, defaulting every column the caller does not name."""
    row = {
        "ledger_id": "c-0001",
        "claim": "a durable fact",
        "topic": "x",
        "first_seen": "2026-06-01",
        "last_reaffirmed": "2026-06-24",
        "restate_count": 1,
        "severity": "normal",
        "status": "standing",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"INSERT INTO claims ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()


def test_load_returns_the_ledger_dict_shape(store):
    _insert(store)
    assert claim_store.load_ledger(store) == {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "a durable fact",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
                "severity": "normal",
                "status": "standing",
                "origin": "extracted",
            }
        ],
    }


def test_load_omits_a_null_column_rather_than_carrying_None(store):
    """The JSON ledger simply lacks a key it never set. A None in its place
    would reach `_coerce_*` helpers and sort keys that never expected one."""
    _insert(store)
    claim = claim_store.load_ledger(store)["claims"][0]
    assert "driver" not in claim
    assert "broke_on" not in claim


def test_load_excludes_retired_claims(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002", retired_on="2026-06-20")
    ids = [c["id"] for c in claim_store.load_ledger(store)["claims"]]
    assert ids == ["c-0001"]


def test_load_maps_broke_on_from_resolved_on(store):
    _insert(store, status="broken", resolved_on="2026-06-22", broken_by_note="a reversal")
    claim = claim_store.load_ledger(store)["claims"][0]
    assert claim["broke_on"] == "2026-06-22"
    assert claim["broken_by"] == "a reversal"


def test_load_orders_by_severity_then_recency_then_id(store):
    """merge_ledger and select_working_set both sort with reverse=True, and
    Python's sort is STABLE -- reverse=True does not reverse equal elements. So
    ties break by INPUT order. Today that is the order merge_ledger last wrote
    to the file; from a database it would be whatever the planner returned, and
    two claims tied on severity and date would swap places between runs."""
    _insert(store, ledger_id="c-0001", severity="normal", last_reaffirmed="2026-06-24")
    _insert(store, ledger_id="c-0002", severity="high", last_reaffirmed="2026-06-01")
    _insert(store, ledger_id="c-0003", severity="normal", last_reaffirmed="2026-06-25")
    _insert(store, ledger_id="c-0004", severity="normal", last_reaffirmed="2026-06-24")
    ids = [c["id"] for c in claim_store.load_ledger(store)["claims"]]
    assert ids == ["c-0002", "c-0003", "c-0001", "c-0004"]


def test_a_null_last_reaffirmed_is_a_hard_error(store):
    """The column is DATE NULL (0006:194), but merge_ledger indexes
    last_reaffirmed directly and select_working_set compares it against "".
    `c.get("last_reaffirmed", "")` returns None when the key is PRESENT AND
    NULL, and the sort then raises TypeError. "Sorts last" holds only for a
    MISSING key. Failing here names the row; failing there names a comparison."""
    _insert(store, last_reaffirmed=None)
    with pytest.raises(ValueError, match="c-0001"):
        claim_store.load_ledger(store)


def test_load_on_an_empty_table_is_an_empty_ledger(store):
    assert claim_store.load_ledger(store) == {"version": 1, "claims": []}


def test_the_sql_severity_order_matches_the_python_rank(store):
    """The store cannot import brief_memory without a cycle, so the ordering is
    duplicated. This test is what stops the duplicate drifting."""
    for name, rank in brief_memory._SEVERITY_RANK.items():
        got = store.execute(
            f"SELECT {claim_store._SEVERITY_ORDER_SQL} FROM (SELECT %s::text AS severity) t",
            (name,),
        ).fetchone()[0]
        assert got == rank, f"{name}: SQL says {got}, Python says {rank}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: a **collection error** — `ModuleNotFoundError: No module named 'claim_store'`, **0 passed**. The import is at the top of the file, so the whole module fails to collect and Task 1's four tests do not run either. That is correct at this point; do not treat the vanished passes as a regression.

- [ ] **Step 3: Create the module**

Create `claim_store.py`:

```python
"""Postgres storage for the standing-claim ledger (news-brief-bqa.10).

The ONLY module that knows SQL, or that the ledger's `broke_on` is the column
`resolved_on`. It owns rows, marshalling and id allocation, and no business
logic: `merge_ledger` and `select_working_set` run unchanged on the dicts it
returns. Deliberately does NOT import brief_memory -- brief.py wires the two
together -- because the reverse edge would be a cycle.

Spec: docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md
"""

from common import log

# Ledger key -> claims column. Three renames (DDL spec 5.1); `kind` is absent on
# purpose, being an admission guard that is never stored.
_KEY_TO_COLUMN = {
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
_DATE_KEYS = ("first_seen", "last_reaffirmed", "resolution_date", "broke_on")

# Mirrors brief_memory._SEVERITY_RANK, which cannot be imported without a cycle.
# ELSE 1 reproduces _severity_rank's "unknown/missing -> normal".
_SEVERITY_ORDER_SQL = (
    "CASE severity WHEN 'high' THEN 2 WHEN 'low' THEN 0 ELSE 1 END"
)


def _row_to_claim(row) -> dict:
    """One database row as the dict merge_ledger expects.

    A NULL column is OMITTED rather than carried as None, because that is what
    the JSON ledger did: a key it never set was simply absent, and the coercion
    helpers are all written for a missing key, not a null one.
    """
    claim = {}
    for key, value in zip(_KEY_TO_COLUMN, row):
        if value is None:
            continue
        claim[key] = value.strftime("%Y-%m-%d") if key in _DATE_KEYS else value
    if "last_reaffirmed" not in claim:
        raise ValueError(
            f"claim {claim.get('id')!r} has a NULL last_reaffirmed. merge_ledger "
            "indexes that key directly and select_working_set compares it to a "
            "string, so a null reaches a sort as None and raises TypeError far "
            "from here. The column is nullable for KB-native rows; ledger rows "
            "may not use that latitude (news-brief-bqa.10 spec 3.4)."
        )
    return claim


def load_ledger(conn) -> dict:
    """Every live claim, in the dict shape brief_memory reads.

    ORDER BY is not decoration. Both merge_ledger's and select_working_set's
    sorts are STABLE and use reverse=True, which does not reverse equal
    elements -- so ties break by input order. Reproducing the order
    merge_ledger last wrote keeps that deterministic instead of leaving it to
    the planner.
    """
    columns = ", ".join(_KEY_TO_COLUMN.values())
    rows = conn.execute(
        f"SELECT {columns} FROM claims WHERE retired_on IS NULL "
        f"ORDER BY {_SEVERITY_ORDER_SQL} DESC, last_reaffirmed DESC, ledger_id"
    ).fetchall()
    return {"version": 1, "claims": [_row_to_claim(r) for r in rows]}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: **12 passed.**

- [ ] **Step 5: Allowlist the new module in the Dockerfile**

In `Dockerfile:35`, add `claim_store.py` to the `COPY` list:

```dockerfile
COPY common.py trading.py polygram_live.py validation.py brief.py brief_memory.py claim_store.py claim_verify.py retention.py db.py scheduler.py supervisor.py backup.py .
```

- [ ] **Step 6: Allowlist it in the workflow, in both places**

In `.github/workflows/docker-publish.yml`, add to the `paths:` list after line 8:

```yaml
      - 'claim_store.py'
```

And to **both** ruff lines (71 and 72), inserting `claim_store.py` after `brief_memory.py`:

```yaml
          ruff check brief.py brief_memory.py claim_store.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py backup.py enrichment scripts tests
          ruff format --check brief.py brief_memory.py claim_store.py claim_verify.py retention.py common.py trading.py polygram_live.py validation.py db.py scheduler.py supervisor.py backup.py enrichment scripts tests
```

- [ ] **Step 7: Verify the allowlist is complete**

```bash
grep -c claim_store Dockerfile .github/workflows/docker-publish.yml
```
Expected: `Dockerfile:1` and `.github/workflows/docker-publish.yml:3`. Anything less means the module is invisible to the image or to CI lint — CI tests pass on a full checkout, so this failure surfaces only in production.

- [ ] **Step 8: Run the full gate and commit**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1325 passed** (1317 + 8).

```bash
git add claim_store.py tests/test_claim_store.py Dockerfile .github/workflows/docker-publish.yml
git commit -m "feat(kb): the ledger can be read out of Postgres, in the shape it had on disk"
```

---

### Task 3: Id allocation that spans retired rows

**Files:**
- Modify: `claim_store.py`
- Modify: `brief_memory.py:289` (`merge_ledger`), `brief_memory.py:785-797` (`reconcile_ledger`)
- Test: `tests/test_claim_store.py`, `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: Task 2's module.
- Produces: `claim_store.next_ledger_num(conn) -> int`; `merge_ledger(..., *, next_num: int | None = None)`; `reconcile_ledger(..., *, next_num: int | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claim_store.py`:

```python
def test_next_ledger_num_counts_retired_rows(store):
    """The collision this exists to prevent is SILENT. load_ledger hides retired
    rows, so _max_id_num(prior)+1 would reissue c-0050, and an upsert keyed
    ON CONFLICT (ledger_id) resolves to the retired row and OVERWRITES it --
    handing a brand-new claim the retired one's first_seen and history."""
    _insert(store, ledger_id="c-0049")
    _insert(store, ledger_id="c-0050", retired_on="2026-06-20")
    assert claim_store.next_ledger_num(store) == 51


def test_next_ledger_num_on_an_empty_table_is_one(store):
    """ledger_id is TEXT NULL (0006:159), so a bare MAX() returns NULL here and
    f"c-{None:04d}" raises. COALESCE is load-bearing, not decoration."""
    assert claim_store.next_ledger_num(store) == 1


def test_next_ledger_num_ignores_rows_without_a_ledger_id(store):
    """bqa.4b will write KB-native claims with no ledger_id at all."""
    _insert(store, ledger_id="c-0007")
    store.execute(
        "INSERT INTO claims (claim, first_seen, last_reaffirmed) "
        "VALUES ('kb-native', '2026-06-01', '2026-06-01')"
    )
    store.commit()
    assert claim_store.next_ledger_num(store) == 8
```

Append to `tests/test_brief_memory.py`:

```python
def test_merge_accepts_an_externally_allocated_next_num():
    """After the cutover, ids are allocated against the whole table including
    retired rows, so the caller supplies the number."""
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(
        prior, [{"claim": "a new fact"}], "2026-06-24", next_num=51
    )
    assert out["claims"][0]["id"] == "c-0051"


def test_merge_still_allocates_from_prior_when_not_told():
    prior = {"version": 1, "claims": [_mk_claim("c-0003")]}
    out = bm.merge_ledger(prior, [{"claim": "a new fact"}], "2026-06-24")
    assert {c["id"] for c in out["claims"]} == {"c-0003", "c-0004"}


def test_reconcile_passes_next_num_through_to_merge():
    """Production does not call merge_ledger -- brief.py calls reconcile_ledger,
    which called merge_ledger with no kwargs. Adding the parameter to
    merge_ledger alone would leave it UNREACHABLE from the only call site that
    matters, and the collision would happen with every test passing."""
    out = bm.reconcile_ledger(
        {"version": 1, "claims": []},
        "a brief",
        "2026-06-24",
        call=lambda system, prompt: '[{"claim": "a new fact"}]',
        next_num=77,
    )
    assert out["claims"][0]["id"] == "c-0077"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_claim_store.py tests/test_brief_memory.py -q`
Expected: 6 failed — `next_ledger_num` undefined, and `merge_ledger`/`reconcile_ledger` reject an unexpected keyword argument.

- [ ] **Step 3: Add `next_ledger_num` to the store**

Append to `claim_store.py`:

```python
def next_ledger_num(conn) -> int:
    """The next `c-NNNN` number, counted across ALL rows including retired ones.

    There is no WHERE clause on purpose. load_ledger hides retired rows, so an
    id computed from what it returns would reissue a retired claim's ledger_id,
    and the upsert would then resolve ON CONFLICT to that row and overwrite it.
    COALESCE covers an empty table and one holding only KB-native rows, where
    MAX() over a nullable TEXT column returns NULL.
    """
    return conn.execute(
        "SELECT COALESCE(MAX(SUBSTRING(ledger_id FROM 'c-(\\d+)')::int), 0) + 1 "
        "FROM claims"
    ).fetchone()[0]
```

- [ ] **Step 4: Thread the parameter through both functions**

In `brief_memory.py`, add `next_num: int | None = None` as the last keyword-only parameter of `merge_ledger`, and replace line 289:

```python
    next_num = _max_id_num(prior) + 1 if next_num is None else next_num
```

Add the same keyword-only parameter to `reconcile_ledger` and pass it on:

```python
        return merge_ledger(
            prior, parse_reconcile_response(text), today, next_num=next_num
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_claim_store.py tests/test_brief_memory.py -q`
Expected: **265 passed** (247 + 3 new brief_memory + 15 store).

- [ ] **Step 6: Run the full gate and commit**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1331 passed** (1325 + 6).

```bash
git add claim_store.py brief_memory.py tests/test_claim_store.py tests/test_brief_memory.py
git commit -m "feat(kb): a retired claim keeps its number, so nothing reissues it"
```

---

### Task 4: `claim_store.save_ledger` — the write path

**Files:**
- Modify: `claim_store.py`
- Test: `tests/test_claim_store.py`

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces: `claim_store.save_ledger(conn, before: dict, after: dict, today: str) -> tuple[int, int]` returning `(written, retired)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claim_store.py`:

```python
_ALL_KEYS = {
    "id": "c-0001",
    "claim": "a durable fact",
    "topic": "energy",
    "first_seen": "2026-06-01",
    "last_reaffirmed": "2026-06-24",
    "restate_count": 3,
    "source_count": 5,
    "severity": "high",
    "origin": "extracted",
    "driver": "a mechanism",
    "horizon_days": 180,
    "resolution_date": "2026-12-01",
    "horizon_elapsed": 23,
    "status": "broken",
    "broke_on": "2026-06-22",
    "broken_by": "a reversal",
    "extractor_model": "claude-haiku-4-5-20251001",
    "prompt_version": 5,
}


def test_all_eighteen_ledger_keys_survive_a_round_trip(store):
    """DDL spec 5.1's full superset, both directions (obligation 2). A key that
    silently fails to persist is invisible until an audit needs it."""
    after = {"version": 1, "claims": [dict(_ALL_KEYS)]}
    claim_store.save_ledger(store, {"version": 1, "claims": []}, after, "2026-06-24")
    assert claim_store.load_ledger(store)["claims"][0] == _ALL_KEYS


def test_save_writes_only_the_rows_that_changed(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [dict(c) for c in before["claims"]]}
    after["claims"][0]["topic"] = "changed"
    written, retired = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (1, 0)


def test_a_row_the_merge_dropped_is_retired_not_deleted(store):
    _insert(store, ledger_id="c-0001")
    _insert(store, ledger_id="c-0002")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [before["claims"][0]]}
    written, retired = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (0, 1)
    row = store.execute(
        "SELECT retired_on FROM claims WHERE ledger_id = 'c-0002'"
    ).fetchone()
    assert row[0].strftime("%Y-%m-%d") == "2026-06-25"


def test_a_retired_row_leaves_the_ledger_but_not_the_table(store):
    _insert(store, ledger_id="c-0001")
    before = claim_store.load_ledger(store)
    claim_store.save_ledger(store, before, {"version": 1, "claims": []}, "2026-06-25")
    assert claim_store.load_ledger(store)["claims"] == []
    assert store.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_an_id_echoed_twice_in_one_reply_becomes_one_row(store):
    """merge_ledger:294 re-enters the `cid in by_id` branch without checking
    `returned`, so a reply echoing one id twice yields two dicts carrying it.
    Harmless in a JSON list; two upserts on one unique key here."""
    _insert(store, ledger_id="c-0001")
    before = claim_store.load_ledger(store)
    twin = dict(before["claims"][0])
    twin["topic"] = "second"
    after = {"version": 1, "claims": [before["claims"][0], twin]}
    written, retired = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert (written, retired) == (1, 0)
    assert claim_store.load_ledger(store)["claims"][0]["topic"] == "second"


def test_an_ordinary_reaffirm_does_not_trip_the_immutability_trigger(store):
    """claims_freeze_claim_text_trg (0006:274) is BEFORE UPDATE FOR EACH ROW, so
    ON CONFLICT DO UPDATE fires it on every reaffirm. save_ledger is the first
    UPDATE writer against this table; nothing had exercised that interaction."""
    _insert(store, ledger_id="c-0001", status="standing")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [dict(before["claims"][0], last_reaffirmed="2026-06-25")]}
    written, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert written == 1


def test_rewriting_a_broken_claim_is_refused_by_the_trigger(store):
    """merge_ledger already refuses this (jx9.5), so the store should never send
    it. The trigger is defence in depth, and this records that the store's
    per-row transaction turns its RAISE into one skipped row rather than a lost
    day of claims."""
    _insert(store, ledger_id="c-0001", status="broken", resolved_on="2026-06-22",
            broken_by_note="a reversal")
    before = claim_store.load_ledger(store)
    after = {"version": 1, "claims": [dict(before["claims"][0], claim="rewritten")]}
    written, _ = claim_store.save_ledger(store, before, after, "2026-06-25")
    assert written == 0
    assert claim_store.load_ledger(store)["claims"][0]["claim"] == "a durable fact"


def test_one_rejected_row_does_not_cost_the_others(store):
    """Per-row transactions, following config.py:605-613. A single transaction
    around the batch would discard every good row for one bad one."""
    good = dict(_ALL_KEYS)
    bad = dict(_ALL_KEYS, id="c-0002", status="broken", broke_on=None, broken_by=None)
    after = {"version": 1, "claims": [good, bad]}
    written, _ = claim_store.save_ledger(
        store, {"version": 1, "claims": []}, after, "2026-06-24"
    )
    assert written == 1
    assert [c["id"] for c in claim_store.load_ledger(store)["claims"]] == ["c-0001"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: 6 failed with `AttributeError: module 'claim_store' has no attribute 'save_ledger'`.

- [ ] **Step 3: Implement the write path**

Append to `claim_store.py`:

```python
def _upsert_sql(keys) -> str:
    """An INSERT naming exactly the columns this claim actually has.

    NOT a fixed eighteen-column statement with NULLs for the absent keys. An
    explicit NULL OVERRIDES a column DEFAULT and then violates NOT NULL -- it
    does not fall back to the default. `status`, `origin`, `severity` and
    `restate_count` are all NOT NULL DEFAULT on `claims`, and the measured
    legacy rows carry none of the first two (spec 5.1), so the fixed form would
    raise 23502 on every row of the real ledger and the per-row `except` would
    swallow all of it into a silent zero.

    Omitting a column from the UPDATE branch leaves its stored value alone,
    which is correct here: merge_ledger never deletes a key from a row (it
    copies with `dict(...)` and only ever sets), so a key absent from `after`
    was absent from `before` too.
    """
    cols = [_KEY_TO_COLUMN[k] for k in keys]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "ledger_id")
    conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return (
        f"INSERT INTO claims ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (ledger_id) {conflict}"
    )


def _write_claim(conn, claim: dict) -> None:
    """Upsert one claim. Dates go back as `%Y-%m-%d` strings; psycopg casts
    them to DATE."""
    keys = [k for k in _KEY_TO_COLUMN if k in claim]
    conn.execute(_upsert_sql(keys), tuple(claim[k] for k in keys))


def save_ledger(conn, before: dict, after: dict, today: str) -> tuple[int, int]:
    """Persist the merge's result. Returns (rows written, rows retired).

    `before` is the ledger as loaded; `after` is what merge_ledger returned.
    A row present in `before` and absent from `after` was dropped by the TTL --
    the only way a row leaves merge_ledger -- so it is RETIRED rather than
    deleted, which keeps claim_evidence, thesis_claims and story_members intact
    (spec 2.1).

    Each row gets its own transaction, following config.py:605-613: one row the
    schema rejects must not cost the operator the rest of the day's claims.
    """
    before_by_id = {c["id"]: c for c in before.get("claims", []) if c.get("id")}
    # Last write wins: merge_ledger can emit one id twice (see :294).
    after_by_id = {c["id"]: c for c in after.get("claims", []) if c.get("id")}

    written = 0
    for cid, claim in after_by_id.items():
        if before_by_id.get(cid) == claim:
            continue
        try:
            with conn.transaction():
                _write_claim(conn, claim)
            written += 1
        except Exception:
            log.exception(f"Claim store: skipped an unwritable claim {cid!r}")

    retired = 0
    for cid in before_by_id.keys() - after_by_id.keys():
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE claims SET retired_on = %s "
                    "WHERE ledger_id = %s AND retired_on IS NULL",
                    (today, cid),
                )
            retired += 1
        except Exception:
            log.exception(f"Claim store: could not retire {cid!r}")
    conn.commit()
    return written, retired
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: **23 passed.**

- [ ] **Step 5: Run the full gate and commit**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1339 passed** (1331 + 8).

```bash
git add claim_store.py tests/test_claim_store.py
git commit -m "feat(kb): the ledger writes back as rows, and a drop becomes a retirement"
```

---

### Task 5: The legacy import

**Files:**
- Modify: `claim_store.py`
- Modify: `supervisor.py:401`
- Test: `tests/test_claim_store.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: `claim_store.import_legacy(conn, path=None) -> int`; `claim_store.LEGACY_LEDGER_FILE: Path`.

- [ ] **Step 1: Write the failing tests**

Add `import json` to the import block at the **top** of `tests/test_claim_store.py`, then
append:

```python
def _write_ledger(tmp_path, claims):
    p = tmp_path / "brief_memory.json"
    p.write_text(json.dumps({"version": 1, "claims": claims}), encoding="utf-8")
    return p


def test_import_accepts_the_eight_key_shape_the_real_ledger_has(store, tmp_path):
    """Measured against from-server/brief_memory.json: 25 rows, and `status`,
    `origin`, `driver`, `horizon_days`, `extractor_model` and `prompt_version`
    absent on ALL of them. The sibling importers' entry["name"] shape would
    fail on every row."""
    p = _write_ledger(tmp_path, [{
        "id": "c-0001", "claim": "a fact", "topic": "x",
        "first_seen": "2026-06-01", "last_reaffirmed": "2026-06-24",
        "restate_count": 1, "source_count": 2, "severity": "normal",
    }])
    assert claim_store.import_legacy(store, p) == 1
    claim = claim_store.load_ledger(store)["claims"][0]
    assert claim["status"] == "standing"
    assert claim["origin"] == "extracted"
    assert "extractor_model" not in claim


def test_import_is_idempotent_because_it_guards_on_the_table(store, tmp_path):
    p = _write_ledger(tmp_path, [{
        "id": "c-0001", "claim": "a fact", "first_seen": "2026-06-01",
        "last_reaffirmed": "2026-06-24",
    }])
    assert claim_store.import_legacy(store, p) == 1
    assert claim_store.import_legacy(store, p) == 0


def test_an_empty_claims_list_is_not_mistaken_for_already_imported(store, tmp_path):
    """The guard reads the TABLE, not the file: the ledger is a dict with a
    claims list, not a bare list, so `{"claims": []}` must import nothing and
    still leave the table importable later."""
    assert claim_store.import_legacy(store, _write_ledger(tmp_path, [])) == 0
    p = _write_ledger(tmp_path, [{
        "id": "c-0001", "claim": "a fact", "first_seen": "2026-06-01",
        "last_reaffirmed": "2026-06-24",
    }])
    assert claim_store.import_legacy(store, p) == 1


def test_a_malformed_file_imports_nothing_and_does_not_raise(store, tmp_path):
    """It runs at boot. It must not be able to stop one."""
    p = tmp_path / "brief_memory.json"
    p.write_text("{not json", encoding="utf-8")
    assert claim_store.import_legacy(store, p) == 0


def test_a_missing_file_imports_nothing(store, tmp_path):
    assert claim_store.import_legacy(store, tmp_path / "nope.json") == 0


def test_a_non_standing_row_without_broke_on_gets_an_approximate_date(store, tmp_path):
    """CHECK (status = 'standing' OR resolved_on IS NOT NULL) (0006:209) would
    reject it, and a per-row skip is silent. _apply_status only began stamping
    broke_on when jx9.x shipped, so such rows can exist. An approximate date
    that is logged beats an invented one, and beats a dropped row."""
    p = _write_ledger(tmp_path, [{
        "id": "c-0001", "claim": "a fact", "first_seen": "2026-06-01",
        "last_reaffirmed": "2026-06-24", "status": "broken",
        "broken_by": "a reversal",
    }])
    assert claim_store.import_legacy(store, p) == 1
    assert claim_store.load_ledger(store)["claims"][0]["broke_on"] == "2026-06-24"


def test_a_row_without_an_id_is_rejected_loudly(store, tmp_path):
    """merge_ledger re-appends an id-less prior row (it is excluded from by_id
    but caught by the trailing loop), so one can exist in the file. It has no
    ledger_id to upsert against and would vanish silently in the diff."""
    p = _write_ledger(tmp_path, [
        {"claim": "no id", "first_seen": "2026-06-01", "last_reaffirmed": "2026-06-24"},
        {"id": "c-0002", "claim": "fine", "first_seen": "2026-06-01",
         "last_reaffirmed": "2026-06-24"},
    ])
    assert claim_store.import_legacy(store, p) == 1
    assert [c["id"] for c in claim_store.load_ledger(store)["claims"]] == ["c-0002"]


def test_import_defaults_a_missing_last_reaffirmed_to_first_seen(store, tmp_path):
    """load_ledger treats a NULL last_reaffirmed as a hard error, so the import
    must never create one."""
    p = _write_ledger(tmp_path, [
        {"id": "c-0001", "claim": "a fact", "first_seen": "2026-06-01"},
    ])
    assert claim_store.import_legacy(store, p) == 1
    assert claim_store.load_ledger(store)["claims"][0]["last_reaffirmed"] == "2026-06-01"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: 8 failed with `AttributeError: module 'claim_store' has no attribute 'import_legacy'`.

- [ ] **Step 3: Implement the import**

Add to `claim_store.py`'s imports, then append the function:

```python
import json
from pathlib import Path

import db
from common import DATA_DIR

LEGACY_LEDGER_FILE = DATA_DIR / "brief_memory.json"
_IMPORT_LOCK = "claim_ledger_import"
```

```python
def import_legacy(conn, path: Path | None = None) -> int:
    """Copy brief_memory.json into `claims`, once, while the table is empty.

    Same emptiness guard as the four sibling importers and for the same reason:
    it makes the import idempotent, so rollback means keeping the FILE rather
    than restoring a backup. The file is never written to and never deleted.

    The guard reads the TABLE, not the file: this ledger is a dict wrapping a
    claims list, so an empty list must not be read as "already imported".

    A malformed file imports nothing and logs -- it runs at boot and must not be
    able to stop one. Rows are validated by the schema and skipped individually,
    and the counts are compared afterwards so a silent per-row rejection shows
    up as a number rather than as nothing.
    """
    path = path or LEGACY_LEDGER_FILE
    with db.advisory_lock(conn, _IMPORT_LOCK) as got:
        if not got:
            return 0
        if conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, json.JSONDecodeError):
            log.exception(f"Could not read {path}; importing no claims")
            return 0
        claims = raw.get("claims") if isinstance(raw, dict) else None
        if not isinstance(claims, list):
            log.warning(f"{path} has no claims list; importing no claims")
            return 0

        # Pre-register before writing: what the file says should land.
        expected = [c for c in claims if isinstance(c, dict) and c.get("id")]
        for c in claims:
            if isinstance(c, dict) and not c.get("id"):
                # merge_ledger re-appends an id-less prior row, so one can
                # reach the file. It has no ledger_id to upsert against and
                # would otherwise vanish between two counts.
                log.warning(
                    "Claim import: skipped a row with no id: "
                    f"{str(c.get('claim'))[:80]!r}"
                )
        coverage = {}
        for c in expected:
            for key in c:
                coverage[key] = coverage.get(key, 0) + 1
        log.info(
            f"Claim import: {len(claims)} entries, {len(expected)} with an id; "
            f"key coverage {dict(sorted(coverage.items()))}"
        )

        imported = 0
        for entry in expected:
            row = dict(entry)
            # load_ledger treats a NULL last_reaffirmed as a hard error.
            row.setdefault("last_reaffirmed", row.get("first_seen"))
            status = row.get("status") or "standing"
            if status != "standing" and not row.get("broke_on"):
                # CHECK (status = 'standing' OR resolved_on IS NOT NULL). Rows
                # predating jx9.x's broke_on stamping would be rejected, and a
                # per-row skip is silent.
                row["broke_on"] = row["last_reaffirmed"]
                log.warning(
                    f"Claim import: {row['id']} is {status} with no broke_on; "
                    f"approximating resolved_on as {row['broke_on']}"
                )
            try:
                with conn.transaction():
                    _write_claim(conn, row)
                imported += 1
            except Exception:
                log.exception(f"Skipped an unimportable claim: {entry.get('id')!r}")
        conn.commit()

    landed = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
    if landed != len(expected):
        log.error(
            f"Claim import variance: expected {len(expected)} rows, {landed} landed. "
            "The difference was rejected individually -- see the skip lines above."
        )
    if imported:
        log.info(f"Imported {imported} claims from {path}")
    return imported
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_claim_store.py -q`
Expected: **31 passed.**

- [ ] **Step 5: Wire it into the supervisor**

In `supervisor.py`, after line **402** (`config.import_state_from_file(conn)` — the last of the four importers; `:400` is `import_sources_from_file` and `:401` is `import_preferences_from_file`), add:

```python
        claim_store.import_legacy(conn)
```

Add `import claim_store` to the module's imports. It belongs **inside** the existing `try`, alongside its four siblings: a failure here means the deployment does not know what it has already established, which is fail-closed on work and fail-open on the bot, exactly like a failed migration.

- [ ] **Step 6: Run the full gate and commit**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1347 passed** (1339 + 8).

```bash
git add claim_store.py supervisor.py tests/test_claim_store.py
git commit -m "feat(kb): the ledger on disk becomes rows, once, and says what it found"
```

---

### Task 6: Cut the brief over

**Files:**
- Modify: `brief.py:102-108` (imports), `brief.py:3109-3111` (render), `brief.py:3206-3216` (reconcile)
- Modify: `brief_memory.py:126-139` (remove `load_ledger`/`save_ledger`)
- Modify: `tests/test_brief_memory.py:28-56` (remove the three file-I/O tests)
- Test: `tests/test_claim_store.py`

**Interfaces:**
- Consumes: everything above.
- Produces: the cutover. `brief_memory` no longer touches the filesystem.

- [ ] **Step 1: Write the failing test for the degraded notice**

Append to `tests/test_claim_store.py`:

```python
def test_a_degraded_run_says_so_instead_of_vanishing(store):
    """render_established_block returns "" when it has nothing, which removes
    the section entirely -- so a database outage and a genuinely empty ledger
    are BYTE-IDENTICAL to the reader. The brief silently loses its memory,
    re-explains yesterday's facts, and nothing says why. The run row tells the
    operator; this line tells the reader."""
    assert "unavailable" in claim_store.degraded_block().lower()
    assert claim_store.degraded_block() != ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m pytest tests/test_claim_store.py::test_a_degraded_run_says_so_instead_of_vanishing -q`
Expected: FAIL — `module 'claim_store' has no attribute 'degraded_block'`.

- [ ] **Step 3: Add the notice**

Append to `claim_store.py`:

```python
def degraded_block() -> str:
    """What the brief shows when the ledger could not be read.

    An empty string would delete the section, making an outage indistinguishable
    from an empty ledger. One line costs nothing and is the difference between a
    visible failure and an invisible one.
    """
    return (
        "## BACKGROUND ALREADY REPORTED\n"
        "Unavailable this run — the claim store could not be read, so treat "
        "nothing below as already established.\n"
    )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m pytest tests/test_claim_store.py::test_a_degraded_run_says_so_instead_of_vanishing -q`
Expected: PASS.

- [ ] **Step 5: Remove the file-I/O functions and their tests**

Delete `load_ledger` and `save_ledger` from `brief_memory.py:126-139` and `BRIEF_MEMORY_FILE` from line 16. Leave `empty_ledger` — it is still used.

**That strands three imports, not one**, and each is an `F401` that fails the gate:

- `_write_json_atomic` — used only at `:139`, inside `save_ledger`
- `DATA_DIR` — used only at `:16`, to build `BRIEF_MEMORY_FILE`
- `Path` (`from pathlib import Path`, line 9) — used only in the two deleted signatures

So line 14 becomes `from common import ANTHROPIC_HEADERS, log`, and line 9 goes entirely.
Verify with `py -m ruff check brief_memory.py` before moving on — `ANTHROPIC_HEADERS` and
`log` are both still used and must stay.

Delete these three tests from `tests/test_brief_memory.py`: `test_load_missing_returns_empty` (`:28`), `test_load_corrupt_returns_empty` (`:32`), `test_save_then_load_roundtrip` (`:38`). Their cases are already covered in `tests/test_claim_store.py` — the missing-file and corrupt-file cases by `test_a_missing_file_imports_nothing` and `test_a_malformed_file_imports_nothing_and_does_not_raise`, the round trip by `test_all_eighteen_ledger_keys_survive_a_round_trip`.

- [ ] **Step 6: Rewire the render call site**

In `brief.py`, change the import block at `:102-108` to drop `load_ledger` and `save_ledger`:

```python
from brief_memory import (
    is_enabled as brief_memory_enabled,
    reconcile_ledger,
    render_established_block,
)
import claim_store
import db
```

Replace `brief.py:3109-3111` with:

```python
    established_block = ""
    if brief_memory_enabled():
        try:
            with db.connect() as ledger_conn:
                established_block = render_established_block(
                    claim_store.load_ledger(ledger_conn)
                )
        except Exception as e:
            log.error(f"Brief-memory unreadable; brief continues degraded: {e}")
            established_block = claim_store.degraded_block()
```

- [ ] **Step 7: Rewire the reconcile call site**

Replace the block at `brief.py:3206-3216`, **starting at the `if brief_memory_enabled():` on line 3206** — the replacement below re-emits that line, so anchoring on 3207 would duplicate it. It currently reads
`save_ledger(reconcile_ledger(load_ledger(), ...))`, which never holds the prior
state and so cannot hand it to the diff. It becomes three statements:

```python
        if brief_memory_enabled():
            try:
                with db.connect() as ledger_conn:
                    before = claim_store.load_ledger(ledger_conn)
                    after = reconcile_ledger(
                        before,
                        brief,
                        today,
                        source_index=load_source_index(today),
                        next_num=claim_store.next_ledger_num(ledger_conn),
                    )
                    written, retired = claim_store.save_ledger(
                        ledger_conn, before, after, today
                    )
                    log.info(f"Brief-memory: {written} written, {retired} retired")
            except Exception as e:
                log.error(f"Brief-memory reconcile skipped (brief unaffected): {e}")
```

`reconcile_ledger` returns `prior` unchanged on failure, which makes the diff
empty and the write a no-op — the correct degraded behaviour, for free.

- [ ] **Step 8: Run the full gate**

```bash
ruff check . && ruff format --check . && py -m pytest -q
```
Expected: **1345 passed** (1347 + 1 new − 3 removed). 0 skipped, 0 xfailed.

- [ ] **Step 9: Verify the cutover left no file writer behind**

```bash
grep -rn "BRIEF_MEMORY_FILE\|brief_memory.json" --include=*.py . | grep -v from-server
```
Expected: matches only in `claim_store.py` (`LEGACY_LEDGER_FILE`) and `retention.py:7` (a docstring). Any other hit is a second writer to a store that is no longer authoritative.

- [ ] **Step 10: Commit**

```bash
git add brief.py brief_memory.py claim_store.py tests/test_brief_memory.py tests/test_claim_store.py
git commit -m "feat(kb): the brief reads its memory from Postgres, and says when it cannot"
```

---

### Task 7: Close out

- [ ] **Step 1: Correct the bd issue body**

`bqa.10`'s description says "obligations 2-5"; the correction currently lives only in its notes.

```bash
bd update news-brief-bqa.10 --description="Split out of news-brief-bqa.4 on 2026-09-02. brief_memory's JSON ledger becomes rows in the claims table; brief.py holds the prior state and claim_store diffs it. Discharges DDL spec section 6 obligations 2 (date marshalling both directions), 3 (c-0001 ledger_id parsing) and 4 (provenance backfill). Obligation 1 landed in b097a77. Obligations 5 and 6 act on entities and outlets, which this change never writes, and belong to bqa.4b. Design: docs/superpowers/specs/2026-09-02-claim-ledger-cutover-design.md"
```

- [ ] **Step 2: Close the issue and report**

```bash
bd close news-brief-bqa.10 --reason="Cutover shipped: migration 0007, claim_store.py, supervisor import, both brief.py call sites rewired."
bd ready
```

- [ ] **Step 3: Report status without pushing**

```bash
git log --oneline origin/main..HEAD | wc -l
git status --short
docker rm -f nb_bqa10
```

Report the commit count, the final test numbers, and stop. **Do not push** — pushing publishes the image and applies migrations 0006 and 0007 to production on the next host restart, which is the user's decision.

---

## Pre-registered counts

| After task | Expected `pytest -q` | Delta |
|---|---|---|
| baseline | 1313 | — |
| 1 | 1317 | +4 |
| 2 | 1325 | +8 |
| 3 | 1331 | +6 |
| 4 | 1339 | +8 |
| 5 | 1347 | +8 |
| 6 | 1345 | +1 −3 |

Every figure assumes `DATABASE_URL` is exported. **If a run reports skips, the number is meaningless** — the database layer is not executing and the store tests are the whole deliverable. A count that disagrees with this table is a stop: work out which test did not appear before writing more code, because the usual cause is a test that silently failed to collect.
