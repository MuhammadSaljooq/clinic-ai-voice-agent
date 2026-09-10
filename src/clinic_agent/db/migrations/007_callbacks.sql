-- Call-back queue. When no appointment works (or the caller prefers), the agent takes
-- their details and drops a request here for staff to work through.

DO $$ BEGIN
    CREATE TYPE callback_status AS ENUM ('waiting', 'contacted', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS callback_requests (
    id         SERIAL PRIMARY KEY,
    name       TEXT,
    phone      TEXT NOT NULL,
    reason     TEXT,
    status     callback_status NOT NULL DEFAULT 'waiting',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS callback_requests_status ON callback_requests (status, created_at);
