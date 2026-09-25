-- What comprehension spent, one row per model call (spec 2026-09-25 4.3).
-- "What did comprehension cost yesterday" took a billing console and three
-- days to answer on 2026-09-10, because the number was never recorded here.
-- It also feeds the phase-2 batch reservation estimate.
CREATE TABLE comprehend_spend (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    stage         TEXT          NOT NULL CHECK (stage IN ('triage', 'integration')),
    model         TEXT          NOT NULL,
    input_tokens  INTEGER       NOT NULL,
    output_tokens INTEGER       NOT NULL,
    usd           NUMERIC(12,6) NOT NULL,
    batch_id      TEXT          NULL
);
CREATE INDEX comprehend_spend_at ON comprehend_spend (at);
