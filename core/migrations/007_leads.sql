-- Dispatcher (M3): leads, their events, every outbound message, and the Postgres mock CRM.

CREATE TABLE IF NOT EXISTS leads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dedupe_key      text UNIQUE,                 -- email:/phone: (LEAD-005); NULL = can't dedupe
    stage           text NOT NULL DEFAULT 'new',
    source          text,
    payload         jsonb NOT NULL,              -- first delivery, attribution preserved (LEAD-001)
    contact         jsonb,                       -- normalized email/phone + issues (LEAD-010)
    crm_id          text,
    assessment      jsonb,                       -- LeadAssessment (LEAD-002)
    draft           jsonb,                       -- reply draft + channel (LEAD-003)
    approval_id     uuid,
    owner           text,                        -- routed human (LEAD-004)
    opted_out       boolean NOT NULL DEFAULT false,
    human_owner     boolean NOT NULL DEFAULT false,
    last_inbound_at timestamptz,                 -- WhatsApp 24h session window (LEAD-020)
    run_id          uuid,
    received_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead_events (
    id         bigserial PRIMARY KEY,
    lead_id    uuid NOT NULL REFERENCES leads (id),
    kind       text NOT NULL,                    -- delivery, stage, lead_replied, human_takeover, opt_out
    data       jsonb NOT NULL DEFAULT '{}'::jsonb,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_events_lead_idx ON lead_events (lead_id, at);

-- Every message the system sent or tried to send, real or mock (audit; "0 messages after X").
CREATE TABLE IF NOT EXISTS outbox (
    id          bigserial PRIMARY KEY,
    lead_id     uuid REFERENCES leads (id),
    channel     text NOT NULL,                   -- whatsapp | email | slack
    recipient   text NOT NULL,
    body        text NOT NULL,
    template    text,                            -- WhatsApp template name outside the 24h window
    status      text NOT NULL,                   -- sent | blocked_* | failed
    provider_id text,
    idem_key    text UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_lead_idx ON outbox (lead_id, created_at);

-- MockCRM for the Dispatcher (CI default); GoHighLevel replaces it in demo runs.
CREATE TABLE IF NOT EXISTS crm_contacts (
    id          text PRIMARY KEY,
    email       text UNIQUE,
    phone       text UNIQUE,
    fields      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
