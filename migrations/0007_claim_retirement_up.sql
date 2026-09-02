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
