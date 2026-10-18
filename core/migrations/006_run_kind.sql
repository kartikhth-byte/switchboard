-- SHR-018: the demo's daily spend cap counts live runs only; eval runs have their own --max-usd.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'live'
    CHECK (kind IN ('live', 'eval'));
