-- Refuses while any groups/embeddings history exists: a reversal must not
-- silently destroy it (there is no DELETE from these tables, ever), nor strand
-- calibrate spend rows the restored stage CHECK would reject.
DO $$
BEGIN
    -- to_regclass: a partially applied up may never have created the table.
    -- Nested IF, not AND: the inner statement is planned only when reached.
    IF to_regclass('item_embeddings') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_embeddings) THEN
            RAISE EXCEPTION 'item_embeddings has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('item_embed_skips') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_embed_skips) THEN
            RAISE EXCEPTION 'item_embed_skips has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('query_embeddings') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM query_embeddings) THEN
            RAISE EXCEPTION 'query_embeddings has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('embedding_calls') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM embedding_calls) THEN
            RAISE EXCEPTION 'embedding_calls has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('embedding_calibrations') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM embedding_calibrations) THEN
            RAISE EXCEPTION 'embedding_calibrations has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('item_groups') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_groups) THEN
            RAISE EXCEPTION 'item_groups has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('item_group_members') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM item_group_members) THEN
            RAISE EXCEPTION 'item_group_members has rows; refusing to drop it';
        END IF;
    END IF;
    IF to_regclass('comprehend_spend') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM comprehend_spend WHERE stage = 'calibrate') THEN
            RAISE EXCEPTION 'comprehend_spend has calibrate rows; refusing to narrow its stage CHECK';
        END IF;
    END IF;
END $$;

ALTER TABLE comprehend_spend DROP CONSTRAINT IF EXISTS comprehend_spend_stage_check;
ALTER TABLE comprehend_spend ADD CONSTRAINT comprehend_spend_stage_check
    CHECK (stage IN ('triage', 'integration', 'attach', 'seed', 'attach_check'));

DROP INDEX IF EXISTS items_created_at_id;
DROP TABLE IF EXISTS item_group_members;
DROP TABLE IF EXISTS item_groups;
DROP TABLE IF EXISTS embedding_calibrations;
DROP TABLE IF EXISTS embedding_calls;
DROP TABLE IF EXISTS query_embeddings;
DROP TABLE IF EXISTS item_embed_skips;
DROP TABLE IF EXISTS item_embeddings;

DROP EXTENSION IF EXISTS vector;
