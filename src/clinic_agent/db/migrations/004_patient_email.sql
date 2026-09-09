-- Patients can now give an email as a second contact method (optional). First and last
-- name are still stored together in `name` (as "First Last"); phone remains the unique
-- key used to match a caller across calls.

ALTER TABLE patients ADD COLUMN IF NOT EXISTS email TEXT;
