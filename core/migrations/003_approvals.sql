-- SHR-008: human approval for side-effecting actions (approval-required tools, approve_first sends).
CREATE TABLE IF NOT EXISTS approvals (
    id          uuid PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES runs (id),
    agent       text NOT NULL,
    action      text NOT NULL,              -- tool name
    payload     jsonb NOT NULL,             -- validated tool arguments
    expires_at  timestamptz NOT NULL,
    decision    text NOT NULL DEFAULT 'pending'
                CHECK (decision IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by  text,
    decided_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS approvals_pending_idx ON approvals (created_at) WHERE decision = 'pending';
