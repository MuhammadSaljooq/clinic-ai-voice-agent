"""Transactional rental booking, cancellation, and lookup.

The database is the authority: the exclusion constraint decides what actually gets
written, so two callers racing for the last free unit cannot both win. Mirrors
`scheduling/booking.py`: an advisory lock per unit gives concurrent writes one lock
order (so overlaps fail cleanly as UnitTaken instead of deadlocking), and the price is
re-derived from the database as defence in depth against a stale or forged token amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import asyncpg

from clinic_agent.rentals.pricing import quote
from clinic_agent.rentals.tokens import verify_rental_token

# Distinct advisory-lock namespace from the clinic's (19731) so the two never collide.
RENTAL_LOCK_CLASS = 19732


class UnitTaken(Exception):
    """The chosen trailer was booked for overlapping dates while we were talking."""


class RentalNotFound(Exception):
    """No active rental with that id."""


@dataclass(frozen=True, slots=True)
class Booked:
    rental_id: int
    trailer_unit_id: int
    trailer_type_id: int
    customer_id: int
    pickup: date
    return_: date
    total: Decimal
    deposit: Decimal


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


async def _serialize_unit_writes(conn: asyncpg.Connection, unit_id: int) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", RENTAL_LOCK_CLASS, unit_id)


async def _upsert_customer(conn: asyncpg.Connection, name: str, phone: str, email: str | None) -> int:
    return await conn.fetchval(
        """
        INSERT INTO customers (name, phone, email) VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE
        SET name = EXCLUDED.name, email = COALESCE(EXCLUDED.email, customers.email)
        RETURNING id
        """,
        name, phone, email,
    )


async def book(
    pool: asyncpg.Pool,
    *,
    rental_token: str,
    secret: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str | None = None,
    source: str = "voice_agent",
    now: datetime | None = None,
) -> Booked:
    v = verify_rental_token(rental_token, secret, now=now)
    slot = v.slot

    async with pool.acquire() as conn, conn.transaction():
        await _serialize_unit_writes(conn, slot.trailer_unit_id)

        rate_row = await conn.fetchrow(
            "SELECT daily_rate, deposit FROM trailer_types WHERE id = $1 AND active",
            slot.trailer_type_id,
        )
        if rate_row is None:
            raise ValueError(f"trailer type {slot.trailer_type_id} is unknown or inactive")

        # Re-derive the price and refuse if the token disagrees with current config.
        q = quote(rate_row["daily_rate"], rate_row["deposit"], slot.pickup, slot.return_)
        if _cents(q.total) != v.total_cents:
            raise ValueError("token price no longer matches the current rate")

        customer_id = await _upsert_customer(conn, customer_name, customer_phone, customer_email)

        try:
            rental_id = await conn.fetchval(
                """
                INSERT INTO rentals (
                    trailer_unit_id, customer_id, pickup_date, return_date,
                    rental_range, daily_rate, deposit, total_cost, source
                )
                VALUES ($1, $2, $3, $4, daterange($3, $4, '[]'), $5, $6, $7, $8)
                RETURNING id
                """,
                slot.trailer_unit_id, customer_id, slot.pickup, slot.return_,
                rate_row["daily_rate"], q.deposit, q.total, source,
            )
        except (
            asyncpg.exceptions.ExclusionViolationError,
            asyncpg.exceptions.DeadlockDetectedError,
        ) as exc:
            raise UnitTaken(
                f"unit {slot.trailer_unit_id} is no longer free for {slot.pickup}..{slot.return_}"
            ) from exc

    return Booked(
        rental_id=rental_id,
        trailer_unit_id=slot.trailer_unit_id,
        trailer_type_id=slot.trailer_type_id,
        customer_id=customer_id,
        pickup=slot.pickup,
        return_=slot.return_,
        total=q.total,
        deposit=q.deposit,
    )


async def cancel(pool: asyncpg.Pool, rental_id: int) -> None:
    async with pool.acquire() as conn:
        cancelled = await conn.fetchval(
            "UPDATE rentals SET status = 'cancelled' WHERE id = $1 AND status = 'booked' RETURNING id",
            rental_id,
        )
    if cancelled is None:
        raise RentalNotFound(f"no booked rental with id {rental_id}")


@dataclass(frozen=True, slots=True)
class UpcomingRental:
    rental_id: int
    trailer_type_id: int
    type_name: str
    pickup: date
    return_: date
    total: Decimal


async def find_upcoming_rentals(
    pool: asyncpg.Pool, *, phone: str, now: datetime | None = None, limit: int = 5
) -> list[UpcomingRental]:
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    rows = await pool.fetch(
        """
        SELECT r.id, r.pickup_date, r.return_date, r.total_cost,
               tt.id AS type_id, tt.name AS type_name
        FROM rentals r
        JOIN trailer_units u ON u.id = r.trailer_unit_id
        JOIN trailer_types tt ON tt.id = u.trailer_type_id
        JOIN customers c ON c.id = r.customer_id
        WHERE r.status = 'booked' AND c.phone = $1 AND r.return_date >= $2
        ORDER BY r.pickup_date
        LIMIT $3
        """,
        phone, today, limit,
    )
    return [
        UpcomingRental(
            rental_id=r["id"], trailer_type_id=r["type_id"], type_name=r["type_name"],
            pickup=r["pickup_date"], return_=r["return_date"], total=r["total_cost"],
        )
        for r in rows
    ]
