"""Slot tokens must be unforgeable, or the hallucination guarantee is worthless."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clinic_agent.scheduling.models import Slot
from clinic_agent.scheduling.tokens import (
    InvalidSlotToken,
    issue_slot_token,
    verify_slot_token,
)

SECRET = "test-secret-do-not-use-in-production"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
SLOT = Slot(
    provider_id=7,
    appointment_type_id=3,
    start=datetime(2026, 9, 15, 13, 30, tzinfo=UTC),
    end=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
)


def test_round_trip_preserves_every_field():
    token = issue_slot_token(SLOT, SECRET, now=NOW)
    assert verify_slot_token(token, SECRET, now=NOW) == SLOT


def test_tampered_payload_is_rejected():
    """The whole point: editing the time must invalidate the token."""
    token = issue_slot_token(SLOT, SECRET, now=NOW)
    body, signature = token.split(".")
    forged = f"{body[:-2]}XY.{signature}"
    with pytest.raises(InvalidSlotToken, match="signature mismatch|undecodable"):
        verify_slot_token(forged, SECRET, now=NOW)


def test_tampered_signature_is_rejected():
    token = issue_slot_token(SLOT, SECRET, now=NOW)
    body, signature = token.split(".")
    with pytest.raises(InvalidSlotToken, match="signature mismatch"):
        verify_slot_token(f"{body}.{signature[:-2]}XY", SECRET, now=NOW)


def test_token_from_a_different_secret_is_rejected():
    token = issue_slot_token(SLOT, "some-other-secret", now=NOW)
    with pytest.raises(InvalidSlotToken, match="signature mismatch"):
        verify_slot_token(token, SECRET, now=NOW)


def test_expired_token_is_rejected():
    token = issue_slot_token(SLOT, SECRET, ttl_seconds=900, now=NOW)
    later = NOW + timedelta(seconds=901)
    with pytest.raises(InvalidSlotToken, match="expired"):
        verify_slot_token(token, SECRET, now=later)


def test_token_still_valid_at_the_ttl_boundary():
    token = issue_slot_token(SLOT, SECRET, ttl_seconds=900, now=NOW)
    assert verify_slot_token(token, SECRET, now=NOW + timedelta(seconds=900)) == SLOT


@pytest.mark.parametrize("bad", ["", "nodot", "a.b.c", "."])
def test_malformed_tokens_are_rejected(bad):
    with pytest.raises(InvalidSlotToken):
        verify_slot_token(bad, SECRET, now=NOW)
