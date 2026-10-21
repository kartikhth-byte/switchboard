-- Operator calendar of record (VOICE-003, VOICE-004). Holds and bookings share one table so a
-- single exclusion constraint makes overlapping appointments for a stylist impossible, whatever
-- the service durations (stronger than a unique (stylist, start) key: a 2h colour at 10:00 and a
-- 30-min cut at 11:00 overlap without sharing a start time).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS appointments (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stylist_id   text NOT NULL,
    service_id   text NOT NULL,
    period       tstzrange NOT NULL,
    status       text NOT NULL CHECK (status IN ('held', 'confirmed', 'cancelled', 'expired')),
    hold_expires timestamptz,
    caller_name  text,
    caller_phone text,
    call_id      text,
    replaces     uuid REFERENCES appointments(id),
    gcal_event   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT no_overlap EXCLUDE USING gist (stylist_id WITH =, period WITH &&)
        WHERE (status IN ('held', 'confirmed'))
);
CREATE INDEX IF NOT EXISTS appointments_stylist ON appointments (stylist_id, lower(period));

-- Every booking action, for the audit trail (VOICE-017).
CREATE TABLE IF NOT EXISTS booking_audit (
    id             bigserial PRIMARY KEY,
    appointment_id uuid REFERENCES appointments(id),
    action         text NOT NULL,     -- hold, confirm, cancel, reschedule, hold_rejected, expire
    data           jsonb NOT NULL DEFAULT '{}',
    call_id        text,
    at             timestamptz NOT NULL DEFAULT now()
);
