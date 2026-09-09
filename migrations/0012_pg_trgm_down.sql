-- Safe to reverse only while nothing depends on it. A GIN/GiST trigram index
-- or a view referencing similarity() would make DROP EXTENSION fail, which is
-- the correct outcome: Postgres refuses rather than silently removing the
-- operator class an index is built on. Reversal is not attempted with
-- CASCADE, because that would drop those dependents too.
DROP EXTENSION IF EXISTS pg_trgm;
