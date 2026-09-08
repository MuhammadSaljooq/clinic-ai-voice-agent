"""Push config.yaml into the database.

Recurring availability, providers and appointment types are owned by config.yaml;
this makes the database agree with it. Idempotent, so it can run on every boot.
"""

from __future__ import annotations

import asyncpg

from clinic_agent.config import ClinicConfig


async def seed_from_config(pool: asyncpg.Pool, cfg: ClinicConfig) -> None:
    async with pool.acquire() as conn, conn.transaction():
        for provider in cfg.providers:
            await conn.execute(
                """
                INSERT INTO providers (id, name) VALUES ($1, $2)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, active = TRUE
                """,
                provider.id,
                provider.name,
            )

        for appointment_type in cfg.appointment_types:
            await conn.execute(
                """
                INSERT INTO appointment_types
                    (id, name, duration_min, buffer_before_min, buffer_after_min, description)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    duration_min = EXCLUDED.duration_min,
                    buffer_before_min = EXCLUDED.buffer_before_min,
                    buffer_after_min = EXCLUDED.buffer_after_min,
                    description = EXCLUDED.description,
                    active = TRUE
                """,
                appointment_type.id,
                appointment_type.name,
                appointment_type.duration_min,
                appointment_type.buffer_before_min,
                appointment_type.buffer_after_min,
                appointment_type.description,
            )
            await conn.execute(
                "DELETE FROM appointment_type_providers WHERE appointment_type_id = $1",
                appointment_type.id,
            )
            for provider_id in appointment_type.providers:
                await conn.execute(
                    "INSERT INTO appointment_type_providers VALUES ($1, $2)",
                    appointment_type.id,
                    provider_id,
                )

        # Recurring rules are wholly owned by config, so replace rather than merge.
        await conn.execute("DELETE FROM availability_rules")
        for rule in cfg.domain_rules():
            await conn.execute(
                """
                INSERT INTO availability_rules (provider_id, weekday, start_time, end_time)
                VALUES ($1, $2, $3, $4)
                """,
                rule.provider_id,
                rule.weekday,
                rule.start,
                rule.end,
            )

        # Explicit ids were inserted above, so nudge the sequences past them or the
        # next auto-generated insert collides.
        for table in ("providers", "appointment_types"):
            await conn.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    GREATEST((SELECT COALESCE(max(id), 1) FROM {table}), 1)
                )
                """
            )
