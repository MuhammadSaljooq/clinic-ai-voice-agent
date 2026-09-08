"""Persist finished calls.

The transcript is the fastest way to understand why a call went well or badly, so it
is stored rather than only logged. Storage is deliberately minimal: the numbers
involved, when, how it ended, and what was said.
"""

from __future__ import annotations

import json
import logging

import asyncpg

log = logging.getLogger(__name__)


async def record_call(pool: asyncpg.Pool, outcome) -> int | None:
    """Write a finished call. Never raises -- a bookkeeping failure must not surface
    as a call failure, since by this point the call is already over."""
    try:
        return await pool.fetchval(
            """
            INSERT INTO calls (telnyx_call_control_id, from_number, ended_at,
                               outcome, transcript)
            VALUES ($1, $2, now(), $3, $4::jsonb)
            ON CONFLICT (telnyx_call_control_id) DO UPDATE
            SET ended_at = now(),
                outcome = EXCLUDED.outcome,
                transcript = EXCLUDED.transcript
            RETURNING id
            """,
            outcome.call_control_id or None,
            outcome.from_number or None,
            outcome.ended_reason,
            json.dumps(outcome.transcript),
        )
    except Exception:
        log.exception("could not record call %s", getattr(outcome, "call_control_id", "?"))
        return None
