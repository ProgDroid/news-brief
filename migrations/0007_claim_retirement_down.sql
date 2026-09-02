-- Restores 0006's index rather than merely dropping ours: 0006 owns
-- claims_open_resolution, so rolling 0007 back must leave the schema 0006
-- promises, not a schema missing an index it declared.
DROP INDEX IF EXISTS claims_live;
DROP INDEX IF EXISTS claims_open_resolution;
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged');
ALTER TABLE claims DROP COLUMN IF EXISTS retired_on;
