-- Clinic scheduling schema.
--
-- The load-bearing piece is the exclusion constraint on `appointments`. It uses a
-- GENERATED column holding the appointment's *buffered* range, so Postgres itself
-- rejects both hard overlaps AND buffer violations. Application logic cannot forget
-- to check, and two concurrent transactions cannot both win.
--
-- Buffer minutes are denormalised onto each appointment on purpose: they snapshot
-- the policy at booking time, so editing an appointment type later never
-- retroactively invalidates appointments already on the books.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS providers (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    active  BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS appointment_types (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    duration_min       INT NOT NULL CHECK (duration_min > 0),
    buffer_before_min  INT NOT NULL DEFAULT 0 CHECK (buffer_before_min >= 0),
    buffer_after_min   INT NOT NULL DEFAULT 0 CHECK (buffer_after_min >= 0),
    description        TEXT,
    active             BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS appointment_type_providers (
    appointment_type_id INT NOT NULL REFERENCES appointment_types(id) ON DELETE CASCADE,
    provider_id         INT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    PRIMARY KEY (appointment_type_id, provider_id)
);

CREATE TABLE IF NOT EXISTS availability_rules (
    id          SERIAL PRIMARY KEY,
    provider_id INT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    weekday     SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    CHECK (start_time < end_time)
);

CREATE TABLE IF NOT EXISTS availability_exceptions (
    id          SERIAL PRIMARY KEY,
    provider_id INT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    on_date     DATE NOT NULL,
    is_closed   BOOLEAN NOT NULL DEFAULT FALSE,
    start_time  TIME,
    end_time    TIME,
    UNIQUE (provider_id, on_date),
    CHECK (
        is_closed
        OR (start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)
    )
);

CREATE TABLE IF NOT EXISTS patients (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,
    sms_opted_out  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$ BEGIN
    CREATE TYPE appointment_status AS ENUM ('booked', 'cancelled', 'completed', 'no_show');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS appointments (
    id                  SERIAL PRIMARY KEY,
    provider_id         INT NOT NULL REFERENCES providers(id),
    appointment_type_id INT NOT NULL REFERENCES appointment_types(id),
    patient_id          INT NOT NULL REFERENCES patients(id),
    starts_at           TIMESTAMPTZ NOT NULL,
    ends_at             TIMESTAMPTZ NOT NULL,
    buffer_before_min   INT NOT NULL DEFAULT 0 CHECK (buffer_before_min >= 0),
    buffer_after_min    INT NOT NULL DEFAULT 0 CHECK (buffer_after_min >= 0),
    status              appointment_status NOT NULL DEFAULT 'booked',
    source              TEXT NOT NULL DEFAULT 'voice_agent',
    gcal_event_id       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),

    -- The appointment's footprint including turnaround buffers.
    --
    -- Supplied by the application rather than GENERATED, because
    -- `timestamptz - interval` is STABLE (not IMMUTABLE) -- interval arithmetic
    -- depends on the session TimeZone -- and Postgres rejects stable expressions
    -- in generated columns. The CHECK below guarantees the app cannot supply a
    -- range that fails to cover the appointment body, so the exclusion
    -- constraint still enforces buffers, not just raw overlaps.
    blocked_range TSTZRANGE NOT NULL,
    CONSTRAINT blocked_range_covers_body CHECK (blocked_range @> tstzrange(starts_at, ends_at)),

    -- Half-open '[)' ranges mean back-to-back appointments touch without conflicting.
    CONSTRAINT no_double_booking EXCLUDE USING gist (
        provider_id WITH =,
        blocked_range WITH &&
    ) WHERE (status = 'booked')
);

CREATE INDEX IF NOT EXISTS appointments_provider_starts
    ON appointments (provider_id, starts_at) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS appointments_starts_at ON appointments (starts_at);

CREATE TABLE IF NOT EXISTS calls (
    id                     SERIAL PRIMARY KEY,
    telnyx_call_control_id TEXT UNIQUE,
    from_number            TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ,
    outcome                TEXT,
    transcript             JSONB,
    disclosure_spoken_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reminders (
    id                  SERIAL PRIMARY KEY,
    -- UNIQUE is the send-once guarantee: the worker can crash, retry, or
    -- double-run and a patient still never gets two texts.
    appointment_id      INT NOT NULL UNIQUE REFERENCES appointments(id) ON DELETE CASCADE,
    scheduled_for       TIMESTAMPTZ NOT NULL,
    sent_at             TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'pending',
    provider_message_id TEXT
);
