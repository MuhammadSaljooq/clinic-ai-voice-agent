"""Rental pricing (pure)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clinic_agent.rentals.pricing import quote, rental_days


def test_days_are_inclusive_of_both_ends():
    assert rental_days(date(2026, 10, 5), date(2026, 10, 5)) == 1   # same day = 1 day
    assert rental_days(date(2026, 10, 5), date(2026, 10, 7)) == 3   # Mon-Wed = 3 days


def test_total_is_days_times_rate_and_deposit_passes_through():
    q = quote(Decimal("45.00"), Decimal("150.00"), date(2026, 10, 5), date(2026, 10, 7))
    assert q.days == 3
    assert q.total == Decimal("135.00")
    assert q.deposit == Decimal("150.00")
