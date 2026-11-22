-- Refuses while evidence rows exist: a reversal must not silently destroy
-- dated history (there is no DELETE from thesis_evidence, ever).
DO $$
BEGIN
    -- to_regclass: a partially applied up may never have created the table.
    -- Nested IF, not AND: the inner statement is planned only when reached.
    IF to_regclass('thesis_evidence') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM thesis_evidence) THEN
            RAISE EXCEPTION 'thesis_evidence has rows; refusing to drop it';
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS thesis_evidence;
