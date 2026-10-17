-- SHR-004: one row per agent run; run_id is the correlation ID for logs, traces and tool calls.
CREATE TABLE IF NOT EXISTS runs (
    id          uuid PRIMARY KEY,
    agent       text NOT NULL,
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'waiting_approval', 'done', 'failed', 'budget_exceeded')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    ended_at    timestamptz,
    tokens_in   integer NOT NULL DEFAULT 0,
    tokens_out  integer NOT NULL DEFAULT 0,
    cost_usd    numeric(12, 6) NOT NULL DEFAULT 0,
    trace_id    text NOT NULL,
    error       text
);

CREATE INDEX IF NOT EXISTS runs_agent_started_idx ON runs (agent, started_at DESC);
