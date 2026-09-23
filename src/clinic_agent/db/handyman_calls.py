"""Persist finished handyman calls (phone and browser console) with their transcript.

Mirrors `db/calls.py` but writes to `handyman_calls` and records a `source` so console
test sessions and real phone calls are distinguishable. A phone call upserts on its Telnyx
id; a console session has none, so it always inserts. Never raises -- by the time this
runs the call is over, and a bookkeeping failure must not surface as a call failure.
"""

from __future__ import annotations

import json
import logging

import asyncpg

log = logging.getLogger(__name__)


async def record_handyman_call(pool: asyncpg.Pool, outcome, *, source: str = "phone") -> int | None:
    control_id = getattr(outcome, "call_control_id", None) or None
    try:
        if control_id:
            return await pool.fetchval(
                """
                INSERT INTO handyman_calls (telnyx_call_control_id, from_number, source,
                                            ended_at, outcome, transcript)
                VALUES ($1, $2, $3, now(), $4, $5::jsonb)
                ON CONFLICT (telnyx_call_control_id) DO UPDATE
                SET ended_at = now(),
                    outcome = EXCLUDED.outcome,
                    transcript = EXCLUDED.transcript
                RETURNING id
                """,
                control_id,
                getattr(outcome, "from_number", None) or None,
                source,
                getattr(outcome, "ended_reason", None),
                json.dumps(getattr(outcome, "transcript", [])),
            )
        return await pool.fetchval(
            """
            INSERT INTO handyman_calls (from_number, source, ended_at, outcome, transcript)
            VALUES ($1, $2, now(), $3, $4::jsonb)
            RETURNING id
            """,
            getattr(outcome, "from_number", None) or None,
            source,
            getattr(outcome, "ended_reason", None),
            json.dumps(getattr(outcome, "transcript", [])),
        )
    except Exception:
        log.exception("could not record handyman call")
        return None
