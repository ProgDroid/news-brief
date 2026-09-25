-- Comprehension cost redesign, phase 1 (spec 2026-09-25 sections 4.4-4.5).
--
-- `stale`: an item older than the candidate window (14 days) when triage
-- reaches it. It cannot be matched against current events -- candidate_events
-- windows on now() -- so it is never integrated. A policy, not a one-way
-- door: deleting the row lets a later backfill re-triage it.
--
-- `quote_page`: common.is_quote_page rejected it structurally, with no model.
-- Held apart from the model's `none` because select_sampled draws its control
-- arm from items the MODEL judged immaterial; a structural reject in that pool
-- would replace the unconfounded control with junk.
ALTER TABLE item_triage DROP CONSTRAINT item_triage_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_verdict_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_reason_check;
ALTER TABLE item_triage ADD CONSTRAINT item_triage_verdict_check
    CHECK (verdict IN ('material', 'immaterial', 'failed', 'stale'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_reason_check
    CHECK (reason IN ('tracked_entity', 'tracked_claim', 'tracked_story',
                      'topical', 'sampled', 'none', 'error',
                      'quote_page', 'stale'));
-- Still biconditional in BOTH directions (see 0009).
ALTER TABLE item_triage ADD CONSTRAINT item_triage_check
    CHECK ((verdict = 'material')
           = (reason NOT IN ('none', 'error', 'quote_page', 'stale')));
-- And a stale verdict carries exactly the stale reason.
ALTER TABLE item_triage ADD CONSTRAINT item_triage_stale_check
    CHECK ((verdict = 'stale') = (reason = 'stale'));
