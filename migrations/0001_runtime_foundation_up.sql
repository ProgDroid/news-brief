-- Runtime foundation: identity, configuration, and the scheduler's run ledger.
-- schema_migrations is created by the runner itself, not here.

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    telegram_chat_id TEXT       NOT NULL,
    timezone        TEXT        NOT NULL DEFAULT 'UTC',
    delivery_time   TIME        NOT NULL DEFAULT '06:00',
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Scoped global (user_id IS NULL) or per-user. Values are TEXT with coercion at
-- read time: typed columns would need a type discriminator plus a cast per read,
-- and every current knob arrives from the environment as a string anyway.
CREATE TABLE settings (
    key        TEXT   NOT NULL,
    user_id    BIGINT NULL REFERENCES users(id) ON DELETE CASCADE,
    value      TEXT   NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX settings_key_global ON settings (key) WHERE user_id IS NULL;
CREATE UNIQUE INDEX settings_key_user ON settings (key, user_id) WHERE user_id IS NOT NULL;

-- One row per attempted run, from EVERY entry path. status and trigger exist
-- from the first migration so on-demand runs never require retrofitting a queue
-- into the table the /jobs command teaches the operator to trust.
CREATE TABLE job_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT        NOT NULL,
    scheduled_for TIMESTAMPTZ NULL,
    trigger       TEXT        NOT NULL CHECK (trigger IN ('scheduled', 'catchup', 'manual')),
    status        TEXT        NOT NULL CHECK (status IN ('queued', 'running', 'finished', 'missed')),
    started_at    TIMESTAMPTZ NULL,
    finished_at   TIMESTAMPTZ NULL,
    exit_code     INTEGER     NULL
);

-- The catch-up rule asks one question on every tick: what is the latest
-- scheduled_for already recorded for this job?
CREATE INDEX job_runs_job_scheduled ON job_runs (job_name, scheduled_for DESC);
CREATE INDEX job_runs_job_started ON job_runs (job_name, started_at DESC);
