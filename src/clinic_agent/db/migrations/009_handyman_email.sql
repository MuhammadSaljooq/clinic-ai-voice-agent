-- A proper booking captures the customer's email alongside the phone. Existing rows keep
-- a NULL email; new bookings taken by the assistant require one (enforced in the agent).
ALTER TABLE handyman_appointment_requests ADD COLUMN IF NOT EXISTS email TEXT;
