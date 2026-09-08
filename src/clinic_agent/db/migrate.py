"""Apply every migration in order.

Numbered filenames are applied ascending, and every file is written to be idempotent
(IF NOT EXISTS, guarded CREATE TYPE), so running this on each boot is safe and keeps
deployment to a single step.
"""

from __future__ import annotations

import logging
import pathlib

import asyncpg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


def migration_files() -> list[pathlib.Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def apply_all(pool: asyncpg.Pool) -> list[str]:
    applied: list[str] = []
    async with pool.acquire() as conn:
        for path in migration_files():
            log.info("applying migration %s", path.name)
            await conn.execute(path.read_text())
            applied.append(path.name)
    return applied
