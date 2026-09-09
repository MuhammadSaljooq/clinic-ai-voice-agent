"""Migration 005 + rental seed against real Postgres."""

from __future__ import annotations

import pathlib
from datetime import date
from decimal import Decimal

import pytest

from clinic_agent.db.rental_seed import seed_rentals_from_config
from clinic_agent.rental_config import load_rental_config

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
async def rental_db(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    await seed_rentals_from_config(pool, cfg)
    return pool, cfg


async def test_seed_creates_types_and_the_right_number_of_units(rental_db):
    pool, cfg = rental_db
    types = await pool.fetch("SELECT id, name, daily_rate FROM trailer_types ORDER BY id")
    assert [t["name"] for t in types] == [t.name for t in cfg.trailer_types]
    assert types[0]["daily_rate"] == Decimal("45.00")
    for t in cfg.trailer_types:
        n = await pool.fetchval(
            "SELECT count(*) FROM trailer_units WHERE trailer_type_id = $1", t.id
        )
        assert n == t.units


async def test_seed_is_idempotent(rental_db):
    pool, cfg = rental_db
    before = await pool.fetchval("SELECT count(*) FROM trailer_units")
    await seed_rentals_from_config(pool, cfg)  # again
    after = await pool.fetchval("SELECT count(*) FROM trailer_units")
    assert before == after


async def test_exclusion_constraint_blocks_double_booking_a_unit(rental_db):
    """The load-bearing safety property: same unit, overlapping dates, one wins."""
    pool, _cfg = rental_db
    unit = await pool.fetchval("SELECT id FROM trailer_units LIMIT 1")
    cust = await pool.fetchval(
        "INSERT INTO customers (name, phone) VALUES ('A','+1') RETURNING id"
    )

    async def rent(pickup, ret):
        await pool.execute(
            "INSERT INTO rentals (trailer_unit_id, customer_id, pickup_date, return_date,"
            " rental_range, daily_rate, total_cost) VALUES ($1,$2,$3,$4,"
            " daterange($3,$4,'[]'), 10, 10)",
            unit, cust, pickup, ret,
        )

    await rent(date(2026, 10, 5), date(2026, 10, 7))
    with pytest.raises(Exception):  # ExclusionViolationError
        await rent(date(2026, 10, 6), date(2026, 10, 8))   # overlaps
    await rent(date(2026, 10, 8), date(2026, 10, 9))        # next day is fine
