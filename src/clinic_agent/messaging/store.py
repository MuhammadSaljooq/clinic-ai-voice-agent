"""Persist and read the SMS message log that backs the operator inbox.

Pure persistence: this module never talks to Telnyx. Sending is orchestrated in
`messaging.outbound` (which records here on success); inbound handling in
`messaging.inbound`; delivery receipts in the messaging webhook. Everything a
conversation needs to render is one `messages` row plus, optionally, a joined
`patients` row for the name and opt-out flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

INBOUND = "inbound"
OUTBOUND = "outbound"


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    phone: str
    name: str | None
    opted_out: bool
    last_body: str
    last_direction: str
    last_kind: str
    last_at: datetime
    total: int
    inbound: int


@dataclass(frozen=True, slots=True)
class Message:
    direction: str
    body: str
    status: str
    kind: str
    created_at: datetime
    error: str | None


@dataclass(frozen=True, slots=True)
class Thread:
    phone: str
    name: str | None
    opted_out: bool
    messages: list[Message]


async def record_inbound(pool: asyncpg.Pool, *, phone: str, body: str, kind: str = "sms") -> int:
    """Store a message we received. It is 'received' the instant it lands."""
    return await pool.fetchval(
        """
        INSERT INTO messages (phone, direction, body, status, kind)
        VALUES ($1, 'inbound', $2, 'received', $3)
        RETURNING id
        """,
        phone,
        body,
        kind,
    )


async def record_outbound(
    pool: asyncpg.Pool,
    *,
    phone: str,
    body: str,
    status: str,
    kind: str = "sms",
    provider_message_id: str | None = None,
    error: str | None = None,
) -> int:
    """Store a message we sent (or tried to). Status is decided by the caller: 'sent'
    once Telnyx accepted it, 'failed' if it raised, 'blocked' if we refused to send
    because the patient opted out."""
    return await pool.fetchval(
        """
        INSERT INTO messages (phone, direction, body, status, kind,
                              provider_message_id, error)
        VALUES ($1, 'outbound', $2, $3, $4, $5, $6)
        RETURNING id
        """,
        phone,
        body,
        status,
        kind,
        provider_message_id,
        error,
    )


async def mark_delivery(
    pool: asyncpg.Pool, *, provider_message_id: str, status: str, error: str | None = None
) -> bool:
    """Advance an outbound message when Telnyx tells us what happened to it.

    Returns whether a row matched -- a receipt for a message we never stored (e.g. a
    voice-fallback text) is simply ignored.
    """
    updated = await pool.fetchval(
        """
        UPDATE messages
        SET status = $2, error = COALESCE($3, error), updated_at = now()
        WHERE provider_message_id = $1
        RETURNING id
        """,
        provider_message_id,
        status,
        error,
    )
    return updated is not None


async def list_threads(pool: asyncpg.Pool, *, limit: int = 100) -> list[ThreadSummary]:
    """One row per phone number, newest conversation first, with a preview and counts."""
    rows = await pool.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (phone)
                   phone, body, direction, kind, created_at
            FROM messages
            ORDER BY phone, created_at DESC
        ),
        counts AS (
            SELECT phone,
                   count(*) AS total,
                   count(*) FILTER (WHERE direction = 'inbound') AS inbound
            FROM messages
            GROUP BY phone
        )
        SELECT l.phone, l.body, l.direction, l.kind, l.created_at,
               c.total, c.inbound,
               pt.name, COALESCE(pt.sms_opted_out, FALSE) AS opted_out
        FROM latest l
        JOIN counts c ON c.phone = l.phone
        LEFT JOIN patients pt ON pt.phone = l.phone
        ORDER BY l.created_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        ThreadSummary(
            phone=r["phone"],
            name=r["name"],
            opted_out=r["opted_out"],
            last_body=r["body"],
            last_direction=r["direction"],
            last_kind=r["kind"],
            last_at=r["created_at"],
            total=r["total"],
            inbound=r["inbound"],
        )
        for r in rows
    ]


async def fetch_thread(pool: asyncpg.Pool, *, phone: str, limit: int = 200) -> Thread:
    """Every message with one number, oldest first (how a conversation reads)."""
    patient = await pool.fetchrow(
        "SELECT name, sms_opted_out FROM patients WHERE phone = $1", phone
    )
    rows = await pool.fetch(
        """
        SELECT direction, body, status, kind, created_at, error
        FROM (
            SELECT direction, body, status, kind, created_at, error
            FROM messages WHERE phone = $1
            ORDER BY created_at DESC
            LIMIT $2
        ) recent
        ORDER BY created_at ASC
        """,
        phone,
        limit,
    )
    return Thread(
        phone=phone,
        name=patient["name"] if patient else None,
        opted_out=bool(patient["sms_opted_out"]) if patient else False,
        messages=[
            Message(
                direction=r["direction"],
                body=r["body"],
                status=r["status"],
                kind=r["kind"],
                created_at=r["created_at"],
                error=r["error"],
            )
            for r in rows
        ],
    )


async def thread_exists(pool: asyncpg.Pool, *, phone: str) -> bool:
    return bool(await pool.fetchval("SELECT 1 FROM messages WHERE phone = $1 LIMIT 1", phone))


async def count_awaiting_reply(pool: asyncpg.Pool) -> int:
    """Threads whose most recent message came from the patient -- i.e. an operator
    could reply. Drives the inbox badge, so it stays a single cheap query."""
    return await pool.fetchval(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT ON (phone) direction
            FROM messages ORDER BY phone, created_at DESC
        ) latest
        WHERE direction = 'inbound'
        """
    ) or 0
