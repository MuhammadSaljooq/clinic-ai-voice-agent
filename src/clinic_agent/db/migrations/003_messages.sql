-- SMS message log: the operator inbox.
--
-- Every SMS the clinic sends or receives is recorded here, so the dashboard can show
-- a real two-way conversation per phone number, and so a dispute about "what were we
-- told / what did we say" can be answered. Reminders are recorded here too (in
-- addition to the `reminders` bookkeeping table) so a patient's thread reads as one
-- continuous conversation rather than three disconnected logs.
--
-- The message text is stored verbatim. Inbound text is written by whoever sent it and
-- is only ever treated as data, never as instructions.

CREATE TABLE IF NOT EXISTS messages (
    id                  SERIAL PRIMARY KEY,

    -- The other party's number, always in the same normalised form we store on
    -- `patients.phone`, so a thread joins cleanly to a patient when one exists.
    phone               TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    body                TEXT NOT NULL,

    -- Delivery lifecycle. Inbound messages are 'received' the moment we store them.
    -- Outbound: 'queued' -> 'sent' -> 'delivered', or 'failed' / 'blocked'
    -- (blocked = we refused to send because the patient opted out).
    status              TEXT NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received', 'queued', 'sent', 'delivered',
                                          'failed', 'blocked')),

    -- Why this message exists: a patient reply, an automatic keyword reply, an
    -- appointment reminder, or a message an operator typed in the dashboard.
    kind                TEXT NOT NULL DEFAULT 'sms'
                        CHECK (kind IN ('sms', 'auto_reply', 'reminder', 'operator')),

    provider_message_id TEXT,          -- Telnyx message id, for delivery-receipt matching
    error               TEXT,          -- populated when status = 'failed'
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Threads are read newest-first per number; the composite index serves both the
-- conversation list ("latest message per phone") and a single thread view.
CREATE INDEX IF NOT EXISTS messages_phone_created ON messages (phone, created_at DESC);
CREATE INDEX IF NOT EXISTS messages_created ON messages (created_at DESC);

-- Delivery receipts arrive out of band and are matched back by provider id.
CREATE INDEX IF NOT EXISTS messages_provider_id
    ON messages (provider_message_id) WHERE provider_message_id IS NOT NULL;
