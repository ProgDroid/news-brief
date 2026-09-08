-- Rows written while the column was nullable have no commitment_state to
-- restore, and inventing one to satisfy NOT NULL is the exact fabrication
-- 0011 exists to prevent. Retire those events rather than guess: the repo
-- retires instead of deleting (claims.retired_on, and the three FKs disagree
-- about deletes), but `events` carries no retirement column, so a reversal
-- must fail loudly rather than quietly fabricate or quietly drop.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM events WHERE commitment_state IS NULL) THEN
        RAISE EXCEPTION
            'Cannot revert 0011: % events have a NULL commitment_state. '
            'Decide what they should be and set them explicitly first.',
            (SELECT count(*) FROM events WHERE commitment_state IS NULL);
    END IF;
END $$;
ALTER TABLE events ALTER COLUMN commitment_state SET NOT NULL;
