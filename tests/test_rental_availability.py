"""Availability against real Postgres."""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from clinic_agent.db.rental_seed import seed_rentals_from_config
from clinic_agent.rental_config import load_rental_config
from clinic_agent.rentals.availability import available_options
from clinic_agent.rentals.tokens import verify_rental_token

REPO = pathlib.Path(__file__).resolve().parents[1]
SECRET = "avail-secret"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def rental_db(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    await seed_rentals_from_config(pool, cfg)
    return pool, cfg


async def _book_unit(pool, type_id, pickup, return_):
    unit = await pool.fetchval(
        "SELECT id FROM trailer_units WHERE trailer_type_id=$1 ORDER BY id LIMIT 1", type_id
    )
    cust = await pool.fetchval(
        "INSERT INTO customers (name, phone) VALUES ('C', '+1"
        + str(pickup.toordinal()) + "') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO rentals (trailer_unit_id, customer_id, pickup_date, return_date,"
        " rental_range, daily_rate, total_cost) VALUES ($1,$2,$3,$4,daterange($3,$4,'[]'),10,10)",
        unit, cust, pickup, return_,
    )


async def test_offers_a_type_with_a_correct_quote_and_signed_token(rental_db):
    pool, _cfg = rental_db
    opts = await available_options(
        pool, _cfg, SECRET, type_id=1, pickup=date(2026, 10, 5), return_=date(2026, 10, 7), now=NOW
    )
    assert len(opts) == 1
    o = opts[0]
    assert o.type_name == "6x12 Utility"
    assert o.days == 3
    assert o.total == Decimal("135.00")     # 3 x 45.00
    v = verify_rental_token(o.token, SECRET, now=NOW)
    assert v.total_cents == 13500


async def test_no_type_returns_one_option_per_available_type(rental_db):
    pool, _cfg = rental_db
    opts = await available_options(
        pool, _cfg, SECRET, type_id=None, pickup=date(2026, 10, 5), return_=date(2026, 10, 7), now=NOW
    )
    assert {o.type_name for o in opts} == {"6x12 Utility", "7x14 Enclosed Cargo"}


async def test_overlap_blocks_but_the_next_day_is_free(rental_db):
    pool, _cfg = rental_db
    # Type 2 has 2 units; book both across 5-7 so it's fully booked those days.
    for _ in range(2):
        unit = await pool.fetchval(
            "SELECT u.id FROM trailer_units u WHERE u.trailer_type_id=2 AND NOT EXISTS"
            " (SELECT 1 FROM rentals r WHERE r.trailer_unit_id=u.id) LIMIT 1"
        )
        cust = await pool.fetchval("INSERT INTO customers (name, phone) VALUES ('C', '+1"
                                   + str(unit) + "') RETURNING id")
        await pool.execute(
            "INSERT INTO rentals (trailer_unit_id, customer_id, pickup_date, return_date,"
            " rental_range, daily_rate, total_cost) VALUES ($1,$2,'2026-10-05','2026-10-07',"
            " daterange('2026-10-05','2026-10-07','[]'),75,225)",
            unit, cust,
        )
    booked = await available_options(pool, _cfg, SECRET, type_id=2,
                                     pickup=date(2026, 10, 6), return_=date(2026, 10, 7), now=NOW)
    assert booked == []                       # fully booked overlapping days
    free = await available_options(pool, _cfg, SECRET, type_id=2,
                                   pickup=date(2026, 10, 8), return_=date(2026, 10, 9), now=NOW)
    assert len(free) == 1                      # next day is available


async def test_invalid_ranges_are_rejected(rental_db):
    pool, _cfg = rental_db
    with pytest.raises(ValueError, match="maximum rental"):
        await available_options(pool, _cfg, SECRET, type_id=1,
                                pickup=date(2026, 10, 5), return_=date(2026, 12, 5), now=NOW)
    with pytest.raises(ValueError, match="too soon"):
        await available_options(pool, _cfg, SECRET, type_id=1,
                                pickup=date(2026, 9, 1), return_=date(2026, 9, 2), now=NOW)
