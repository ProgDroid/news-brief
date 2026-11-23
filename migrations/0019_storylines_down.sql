-- Refuses while any storyline history exists: a reversal must not silently
-- destroy it (there is no DELETE from these tables, ever), nor strand spend
-- rows the restored two-stage CHECK would reject.
DO $$
BEGIN
    -- to_regclass: a partially applied up may never have created the table.
    -- Nested IF, not AND: the inner statement is planned only when reached.
    IF to_regclass('storylines') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM storylines) THEN
            RAISE EXCEPTION 'storylines has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('storyline_proposals') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM storyline_proposals) THEN
            RAISE EXCEPTION 'storyline_proposals has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('item_attach_decisions') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_attach_decisions) THEN
            RAISE EXCEPTION 'item_attach_decisions has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('item_attachments') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_attachments) THEN
            RAISE EXCEPTION 'item_attachments has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('comprehend_spend') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1 FROM comprehend_spend
            WHERE stage IN ('attach', 'seed', 'attach_check')
        ) THEN
            RAISE EXCEPTION 'comprehend_spend has attach/seed/attach_check rows; refusing to narrow its stage CHECK';
        END IF;
    END IF;
END $$;

ALTER TABLE comprehend_spend DROP CONSTRAINT IF EXISTS comprehend_spend_stage_check;
ALTER TABLE comprehend_spend ADD CONSTRAINT comprehend_spend_stage_check
    CHECK (stage IN ('triage', 'integration'));

ALTER TABLE thesis_evidence DROP CONSTRAINT IF EXISTS thesis_evidence_storyline_fk;

DROP TABLE IF EXISTS item_attachments;
DROP TABLE IF EXISTS item_attach_decisions;
DROP TABLE IF EXISTS storyline_proposals;
DROP TABLE IF EXISTS storylines;
