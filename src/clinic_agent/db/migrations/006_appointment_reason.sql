-- The reason for the visit / symptoms the caller mentioned, captured at booking so the
-- clinical team has context. Free text, optional. Never used for advice by the agent.

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reason TEXT;
