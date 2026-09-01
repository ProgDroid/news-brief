-- A queued manual run needs an age.
--
-- The supervisor closes a request that has gone stale rather than firing it
-- hours later, which means measuring how long a row has sat in `queued`. No
-- existing column can carry that: scheduled_for must stay NULL on a manual row
-- or it would consume a scheduled fire time (latest_scheduled_for is
-- max(scheduled_for)), and started_at is not set until the row is claimed.
ALTER TABLE job_runs ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- The scheduler tick reads this on every pass and the queue is nearly always
-- empty, so the index is partial: it stays the size of the backlog, not the
-- size of the ledger.
CREATE INDEX job_runs_queued ON job_runs (created_at) WHERE status = 'queued';
