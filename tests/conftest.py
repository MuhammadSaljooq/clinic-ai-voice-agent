"""Test fixtures backed by a real Postgres.

The exclusion constraint is the core safety property of this system, and it exists
only in Postgres -- so these tests run against the real thing. SQLite has no
equivalent, and mocking the database would test nothing worth testing.

Requires: docker start clinic-pg
"""

from __future__ import annotations

import os
import pathlib

import asyncpg
import pytest

ADMIN_DSN = os.environ.get(
    "ADMIN_DATABASE_URL", "postgresql://clinic:clinic@localhost:55432/postgres"
)
TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://clinic:clinic@localhost:55432/clinic_test"
)
MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1] / "src/clinic_agent/db/migrations/001_init.sql"
)


@pytest.fixture
async def pool():
    """A pool on a freshly migrated clinic_test database."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = 'clinic_test'")
        if not exists:
            await admin.execute("CREATE DATABASE clinic_test")
    finally:
        await admin.close()

    p = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=10)
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(MIGRATION.read_text())
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def clinic(pool):
    """Two providers and two appointment types.

    Type 1 (Follow-up, 30min, 15min after-buffer) -> both providers.
    Type 2 (New patient, 60min, 10min after-buffer) -> provider 1 only,
    so provider-eligibility has something to reject.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO providers (id, name) VALUES (1, 'Dr. Reyes'), (2, 'Dr. Osei')"
        )
        await conn.execute(
            """
            INSERT INTO appointment_types (id, name, duration_min, buffer_after_min)
            VALUES (1, 'Follow-up', 30, 15), (2, 'New patient', 60, 10)
            """
        )
        await conn.execute(
            "INSERT INTO appointment_type_providers VALUES (1, 1), (2, 1), (1, 2)"
        )
    return pool
