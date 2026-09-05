-- The sampled control arm's daily budget (comprehend.select_sampled) must count
-- PROMOTIONS, not triage. `created_at` records when a row was TRIAGED, and
-- record_triage's ON CONFLICT ... DO UPDATE never rewrites it -- so a row
-- triaged yesterday and promoted into the sampled arm today was invisible to
-- today's budget under `created_at`, letting the cap silently unbind across
-- every UTC day boundary (20/fire on an hourly schedule becomes 480/day in
-- the expensive tier, the exact blowup the budget exists to prevent).
ALTER TABLE item_triage ADD COLUMN sampled_at TIMESTAMPTZ NULL;
