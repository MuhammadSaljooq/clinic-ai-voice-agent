"""Send an SMS on the clinic's behalf and record it, honouring opt-out.

Used by the operator inbox. Opt-out is checked here, not just at reminder time, so a
message an operator types by hand cannot text someone who has replied STOP -- the same
legal line the automated reminders respect.
"""

from __future__ import annotations

import logging

import asyncpg

from clinic_agent.messaging import store

log = logging.getLogger(__name__)

# Telnyx delivery-receipt statuses -> our message lifecycle. Anything unmapped leaves
# the row untouched, so an unfamiliar receipt never regresses a delivered message.
DELIVERY_STATUS = {
    "queued": "sent",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "delivery_failed": "failed",
    "sending_failed": "failed",
    "failed": "failed",
}


class SendResult:
    __slots__ = ("message_id", "ok", "reason")

    def __init__(self, ok: bool, *, reason: str | None = None, message_id: int | None = None):
        self.ok = ok
        self.reason = reason
        self.message_id = message_id


async def _opted_out(pool: asyncpg.Pool, phone: str) -> bool:
    return bool(
        await pool.fetchval("SELECT sms_opted_out FROM patients WHERE phone = $1", phone)
    )


async def send_message(
    pool: asyncpg.Pool,
    *,
    telnyx,
    from_number: str,
    to: str,
    text: str,
    kind: str = "operator",
) -> SendResult:
    """Send one message. Records the outcome to the message log either way.

    Refuses (records 'blocked', does not call Telnyx) if the recipient has opted out.
    """
    to = to.strip()
    text = text.strip()
    if not to or not text:
        return SendResult(False, reason="A recipient and a message are both required.")

    if await _opted_out(pool, to):
        await store.record_outbound(
            pool, phone=to, body=text, status="blocked", kind=kind,
            error="recipient has opted out of SMS",
        )
        return SendResult(False, reason="This number has opted out, so nothing was sent.")

    try:
        result = await telnyx.send_sms(to=to, from_=from_number, text=text)
    except Exception as exc:
        # Surface any transport failure as a recorded, failed send rather than a 500.
        log.exception("operator SMS to a patient number failed")
        mid = await store.record_outbound(
            pool, phone=to, body=text, status="failed", kind=kind, error=str(exc)[:500]
        )
        return SendResult(False, reason="Telnyx rejected the message.", message_id=mid)

    provider_id = ((result or {}).get("data") or {}).get("id")
    mid = await store.record_outbound(
        pool, phone=to, body=text, status="sent", kind=kind, provider_message_id=provider_id
    )
    return SendResult(True, message_id=mid)
