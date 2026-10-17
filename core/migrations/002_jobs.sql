-- SHR-007: scheduled work (follow-ups, reminders, callbacks) + failure queue. No Redis, no Celery.
CREATE TABLE IF NOT EXISTS jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL,
    key           text,                       -- dedupe + cancel handle, e.g. lead:42:followup:1
    run_at        timestamptz NOT NULL DEFAULT now(),
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    attempts      integer NOT NULL DEFAULT 0,
    max_attempts  integer NOT NULL DEFAULT 5,
    last_error    text,
    run_id        uuid,                       -- correlation with the run that enqueued it
    locked_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- One live job per key: replays and double-enqueues collapse onto the existing job.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_live_key_uq
    ON jobs (key) WHERE key IS NOT NULL AND status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (run_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS jobs_failed_idx ON jobs (updated_at DESC) WHERE status = 'failed';
