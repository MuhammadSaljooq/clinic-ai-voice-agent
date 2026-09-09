"""Rental pricing. Pure.

Days are inclusive of both the pickup and return day -- you have the trailer through the
day you bring it back -- so a Monday-to-Wednesday rental is three days.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clinic_agent.rentals.models import Quote


def rental_days(pickup: date, return_: date) -> int:
    return (return_ - pickup).days + 1


def quote(daily_rate: Decimal, deposit: Decimal, pickup: date, return_: date) -> Quote:
    days = rental_days(pickup, return_)
    total = (Decimal(days) * daily_rate).quantize(Decimal("0.01"))
    return Quote(days=days, total=total, deposit=deposit)
