-- Reminder bookkeeping.
--
-- `body` stores the exact text sent (or that would have been sent in dry-run), so the
-- wording can be reviewed before a single message reaches a patient, and so a dispute
-- about what someone was told can be answered.

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Partial index: only opted-out patients matter, and they are the minority.
CREATE INDEX IF NOT EXISTS patients_opted_out
    ON patients (id) WHERE sms_opted_out;

CREATE INDEX IF NOT EXISTS reminders_status ON reminders (status);
