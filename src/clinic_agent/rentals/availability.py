"""Find bookable trailer options for a date range.

The only seam between the pure pricing/token logic and the database. Returns at most one
option per trailer type (units of a type are interchangeable), each bound to a specific
free unit and a signed token. Invalid date ranges raise ValueError with a spoken-friendly
message; a type with no free unit is simply omitted.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import asyncpg

from clinic_agent.rental_config import RentalConfig
from clinic_agent.rentals.models import RentalOption
from clinic_agent.rentals.pricing import quote, rental_days
from clinic_agent.rentals.tokens import RentalSlot, issue_rental_token


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def _validate(cfg: RentalConfig, pickup: date, return_: date, today: date) -> None:
    if return_ < pickup:
        raise ValueError("the return date is before the pickup date")
    days = rental_days(pickup, return_)
    rules = cfg.rental
    if days < rules.min_days:
        raise ValueError(f"the minimum rental is {rules.min_days} day(s)")
    if days > rules.max_days:
        raise ValueError(f"the maximum rental is {rules.max_days} day(s)")
    earliest = today.toordinal() + rules.min_lead_days
    if pickup.toordinal() < earliest:
        raise ValueError("that pickup date is too soon")
    if pickup.toordinal() > today.toordinal() + rules.booking_horizon_days:
        raise ValueError(f"we only book up to {rules.booking_horizon_days} days ahead")


async def available_options(
    pool: asyncpg.Pool,
    cfg: RentalConfig,
    secret: str,
    *,
    type_id: int | None,
    pickup: date,
    return_: date,
    now: datetime | None = None,
) -> list[RentalOption]:
    today = (now or datetime.now(cfg.tz)).astimezone(cfg.tz).date()
    _validate(cfg, pickup, return_, today)

    rows = await pool.fetch(
        """
        SELECT u.id AS unit_id, u.trailer_type_id
        FROM trailer_units u
        JOIN trailer_types tt ON tt.id = u.trailer_type_id AND tt.active
        WHERE u.active
          AND ($1::int IS NULL OR u.trailer_type_id = $1)
          AND NOT EXISTS (
              SELECT 1 FROM rentals r
              WHERE r.trailer_unit_id = u.id
                AND r.status = 'booked'
                AND r.rental_range && daterange($2, $3, '[]')
          )
        ORDER BY u.trailer_type_id, u.id
        """,
        type_id, pickup, return_,
    )

    options: list[RentalOption] = []
    seen: set[int] = set()
    for row in rows:
        tid = row["trailer_type_id"]
        if tid in seen:
            continue  # one option per type; units are interchangeable
        t = cfg.type_by_id(tid)
        if t is None:
            continue
        seen.add(tid)
        q = quote(t.daily_rate, t.deposit, pickup, return_)
        slot = RentalSlot(
            trailer_unit_id=row["unit_id"], trailer_type_id=tid, pickup=pickup, return_=return_
        )
        token = issue_rental_token(
            slot, total_cents=_cents(q.total), deposit_cents=_cents(q.deposit),
            secret=secret, now=now,
        )
        options.append(RentalOption(
            trailer_type_id=tid, type_name=t.name, pickup=pickup, return_=return_,
            days=q.days, total=q.total, deposit=q.deposit, token=token,
        ))
    return options
