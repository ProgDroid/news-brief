-- Rows using the new values cannot survive the old constraints. They are
-- regenerable (triage re-derives them), so they are deleted, not rewritten.
DELETE FROM item_triage WHERE verdict = 'stale' OR reason IN ('quote_page', 'stale');
ALTER TABLE item_triage DROP CONSTRAINT item_triage_stale_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_verdict_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_reason_check;
ALTER TABLE item_triage ADD CONSTRAINT item_triage_verdict_check
    CHECK (verdict IN ('material', 'immaterial', 'failed'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_reason_check
    CHECK (reason IN ('tracked_entity', 'tracked_claim', 'tracked_story',
                      'topical', 'sampled', 'none', 'error'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_check
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error')));
