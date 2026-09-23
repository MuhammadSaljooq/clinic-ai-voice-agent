-- Handyman agent (Task Titan) schema. Independent of the clinic and trailer tables.
--
-- There is no calendar or inventory here: a handyman quotes after seeing the job, so the
-- agent records requests, leads, and messages for the owner to action -- nothing is
-- double-booked and no price is committed. Each table carries a status the console cycles.

DO $$ BEGIN
    CREATE TYPE handyman_appt_status AS ENUM ('requested', 'confirmed', 'declined', 'done');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS handyman_appointment_requests (
    id             SERIAL PRIMARY KEY,
    name           TEXT,
    phone          TEXT NOT NULL,
    job_type       TEXT,
    description    TEXT,
    address        TEXT,
    preferred_time TEXT,
    status         handyman_appt_status NOT NULL DEFAULT 'requested',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS handyman_requests_by_status
    ON handyman_appointment_requests (status, created_at DESC);

DO $$ BEGIN
    CREATE TYPE handyman_lead_status AS ENUM ('new', 'contacted', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS handyman_leads (
    id         SERIAL PRIMARY KEY,
    name       TEXT,
    phone      TEXT NOT NULL,
    reason     TEXT,
    status     handyman_lead_status NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS handyman_leads_by_status
    ON handyman_leads (status, created_at DESC);

DO $$ BEGIN
    CREATE TYPE handyman_message_status AS ENUM ('new', 'read');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS handyman_messages (
    id          SERIAL PRIMARY KEY,
    caller_name TEXT,
    phone       TEXT,
    message     TEXT NOT NULL,
    status      handyman_message_status NOT NULL DEFAULT 'new',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS handyman_messages_by_status
    ON handyman_messages (status, created_at DESC);

-- Transcripts. A phone call upserts on its Telnyx id; a browser console session has no id
-- and always inserts, so the Transcriptions page has data before a real line is wired.
CREATE TABLE IF NOT EXISTS handyman_calls (
    id                     SERIAL PRIMARY KEY,
    telnyx_call_control_id TEXT UNIQUE,
    from_number            TEXT,
    source                 TEXT NOT NULL DEFAULT 'phone',
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ,
    outcome                TEXT,
    transcript             JSONB
);
CREATE INDEX IF NOT EXISTS handyman_calls_recent ON handyman_calls (started_at DESC);
