"""Signed rental tokens: round-trip, expiry, tamper."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from clinic_agent.rentals.models import RentalSlot
from clinic_agent.rentals.tokens import (
    InvalidRentalToken,
    issue_rental_token,
    verify_rental_token,
)

SECRET = "rental-test-secret"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
SLOT = RentalSlot(trailer_unit_id=7, trailer_type_id=1, pickup=date(2026, 10, 5), return_=date(2026, 10, 7))


def test_round_trips():
    tok = issue_rental_token(SLOT, total_cents=13500, deposit_cents=15000, secret=SECRET, now=NOW)
    v = verify_rental_token(tok, SECRET, now=NOW)
    assert v.slot == SLOT
    assert v.total_cents == 13500
    assert v.deposit_cents == 15000


def test_expired_token_is_rejected():
    tok = issue_rental_token(SLOT, total_cents=1, deposit_cents=0, secret=SECRET, ttl_seconds=60, now=NOW)
    later = datetime(2026, 9, 14, 12, 2, tzinfo=UTC)
    with pytest.raises(InvalidRentalToken, match="expired"):
        verify_rental_token(tok, SECRET, now=later)


def test_forged_or_tampered_token_is_rejected():
    tok = issue_rental_token(SLOT, total_cents=1, deposit_cents=0, secret=SECRET, now=NOW)
    with pytest.raises(InvalidRentalToken):
        verify_rental_token(tok, "wrong-secret", now=NOW)
    body, _sig = tok.split(".")
    with pytest.raises(InvalidRentalToken):
        verify_rental_token(f"{body}.deadbeef", SECRET, now=NOW)
