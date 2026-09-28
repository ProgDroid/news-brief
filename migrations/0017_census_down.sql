-- Refuses while the census has labels: a migration reversal must not be able
-- to silently destroy work the way DELETE-ing the census_block row alone
-- cannot (that only lifts the retention hold, spec section 7 "Release").
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM census_assignments) THEN
        RAISE EXCEPTION 'the event census has labels; refusing to drop it';
    END IF;
END $$;

DROP TRIGGER IF EXISTS census_hold_truncate_trg ON items;
DROP TRIGGER IF EXISTS census_hold_row_trg ON items;
DROP FUNCTION IF EXISTS census_hold_truncate();
DROP FUNCTION IF EXISTS census_hold_row();

DROP TABLE IF EXISTS census_events;
DROP TABLE IF EXISTS census_sessions;
DROP TABLE IF EXISTS census_adjudications;
DROP TABLE IF EXISTS census_assignments;
DROP TABLE IF EXISTS census_groups;
DROP TABLE IF EXISTS census_window_items;
DROP TABLE IF EXISTS census_windows;
DROP TABLE IF EXISTS census_skipped_windows;
DROP TABLE IF EXISTS census_window_order;
DROP TABLE IF EXISTS census_block;
