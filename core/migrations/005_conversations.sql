-- SHR-013: per-conversation state (memory window, summary, facts, verified identity,
-- pending approval). One row per conversation; no cross-conversation memory.
CREATE TABLE IF NOT EXISTS conversations (
    id          text PRIMARY KEY,
    agent       text NOT NULL,
    state       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
