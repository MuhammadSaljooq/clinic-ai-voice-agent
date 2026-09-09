"""Value objects for the trailer-rental domain. Pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RentalSlot:
    """A specific free unit for a specific date range -- what a signed token binds to."""

    trailer_unit_id: int
    trailer_type_id: int
    pickup: date
    return_: date


@dataclass(frozen=True, slots=True)
class Quote:
    days: int
    total: Decimal
    deposit: Decimal


@dataclass(frozen=True, slots=True)
class RentalOption:
    """One bookable option offered to the caller (returned by find_available_trailers)."""

    trailer_type_id: int
    type_name: str
    pickup: date
    return_: date
    days: int
    total: Decimal
    deposit: Decimal
    token: str
