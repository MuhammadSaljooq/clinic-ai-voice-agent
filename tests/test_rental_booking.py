"""Rental booking against real Postgres. Headline: no double-booking under a race."""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from clinic_agent.db.rental_seed import seed_rentals_from_config
from clinic_agent.rental_config import load_rental_config
from clinic_agent.rentals.availability import available_options
from clinic_agent.rentals.booking import (
    Booked,
    RentalNotFound,
    UnitTaken,
    book,
    cancel,
    find_upcoming_rentals,
)
from clinic_agent.rentals.models import RentalSlot
from clinic_agent.rentals.tokens import issue_rental_token

REPO = pathlib.Path(__file__).resolve().parents[1]
SECRET = "book-secret"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
PICKUP, RETURN = date(2026, 10, 5), date(2026, 10, 7)


@pytest.fixture
async def rental_db(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    await seed_rentals_from_config(pool, cfg)
    return pool, cfg


async def _one_option(pool, cfg, type_id=1):
    opts = await available_options(pool, cfg, SECRET, type_id=type_id, pickup=PICKUP, return_=RETURN, now=NOW)
    return opts[0]


async def test_booking_writes_a_rental(rental_db):
    pool, cfg = rental_db
    opt = await _one_option(pool, cfg)
    result = await book(pool, rental_token=opt.token, secret=SECRET,
                        customer_name="Ada Lovelace", customer_phone="+15551230000",
                        customer_email="ada@example.com", now=NOW)
    assert isinstance(result, Booked)
    assert result.total == Decimal("135.00")
    row = await pool.fetchrow("SELECT status, total_cost, deposit FROM rentals WHERE id=$1", result.rental_id)
    assert row["status"] == "booked"
    assert row["total_cost"] == Decimal("135.00")
    assert row["deposit"] == Decimal("150.00")


async def test_two_concurrent_bookings_of_one_unit_produce_one_winner(rental_db):
    pool, cfg = rental_db
    opt = await _one_option(pool, cfg)          # token bound to one specific unit
    results = await asyncio.gather(
        book(pool, rental_token=opt.token, secret=SECRET, customer_name="A", customer_phone="+1a", now=NOW),
        book(pool, rental_token=opt.token, secret=SECRET, customer_name="B", customer_phone="+1b", now=NOW),
        return_exceptions=True,
    )
    booked = [r for r in results if isinstance(r, Booked)]
    taken = [r for r in results if isinstance(r, UnitTaken)]
    assert len(booked) == 1, f"expected exactly one winner, got {results}"
    assert len(taken) == 1
    assert await pool.fetchval("SELECT count(*) FROM rentals WHERE status='booked'") == 1


async def test_a_token_whose_price_drifted_is_refused(rental_db):
    pool, _cfg = rental_db
    slot = RentalSlot(trailer_unit_id=1, trailer_type_id=1, pickup=PICKUP, return_=RETURN)
    bad = issue_rental_token(slot, total_cents=999, deposit_cents=15000, secret=SECRET, now=NOW)
    with pytest.raises(ValueError, match="price"):
        await book(pool, rental_token=bad, secret=SECRET, customer_name="A", customer_phone="+1", now=NOW)


async def test_cancel_frees_the_dates(rental_db):
    pool, cfg = rental_db
    opt = await _one_option(pool, cfg)
    result = await book(pool, rental_token=opt.token, secret=SECRET, customer_name="A", customer_phone="+1", now=NOW)
    await cancel(pool, result.rental_id)
    with pytest.raises(RentalNotFound):
        await cancel(pool, result.rental_id)
    # unit is bookable again for the same dates
    again = await available_options(pool, cfg, SECRET, type_id=1, pickup=PICKUP, return_=RETURN, now=NOW)
    assert any(o.trailer_type_id == 1 for o in again)


async def test_find_upcoming_rentals_by_phone(rental_db):
    pool, cfg = rental_db
    opt = await _one_option(pool, cfg)
    await book(pool, rental_token=opt.token, secret=SECRET, customer_name="Ada", customer_phone="+15550001", now=NOW)
    found = await find_upcoming_rentals(pool, phone="+15550001", now=NOW)
    assert len(found) == 1
    assert found[0].type_name == "6x12 Utility"
    assert await find_upcoming_rentals(pool, phone="+15559999", now=NOW) == []
