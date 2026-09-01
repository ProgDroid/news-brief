DROP INDEX IF EXISTS job_runs_queued;
ALTER TABLE job_runs DROP COLUMN created_at;
