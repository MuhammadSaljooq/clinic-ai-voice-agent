"""Push trailer_config.yaml into the database. Idempotent, runs on boot.

Trailer types are owned by config. For each type we ensure the configured number of
physical units exist (labelled A, B, C, ...); we never delete units, since a unit may
have rentals against it.
"""

from __future__ import annotations

import string

import asyncpg

from clinic_agent.rental_config import RentalConfig

_LABELS = string.ascii_uppercase


async def seed_rentals_from_config(pool: asyncpg.Pool, cfg: RentalConfig) -> None:
    async with pool.acquire() as conn, conn.transaction():
        for t in cfg.trailer_types:
            await conn.execute(
                """
                INSERT INTO trailer_types (id, name, description, daily_rate, deposit)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    daily_rate = EXCLUDED.daily_rate,
                    deposit = EXCLUDED.deposit,
                    active = TRUE
                """,
                t.id, t.name, t.description, t.daily_rate, t.deposit,
            )
            existing = await conn.fetchval(
                "SELECT count(*) FROM trailer_units WHERE trailer_type_id = $1", t.id
            )
            for i in range(existing, t.units):
                label = _LABELS[i] if i < len(_LABELS) else f"U{i + 1}"
                await conn.execute(
                    "INSERT INTO trailer_units (trailer_type_id, label) VALUES ($1, $2)"
                    " ON CONFLICT (trailer_type_id, label) DO NOTHING",
                    t.id, label,
                )

        # Explicit ids were inserted, so nudge the sequence past them.
        await conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('trailer_types', 'id'),
                GREATEST((SELECT COALESCE(max(id), 1) FROM trailer_types), 1)
            )
            """
        )
