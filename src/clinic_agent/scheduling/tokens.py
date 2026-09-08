"""Opaque, signed slot tokens.

The language model must never construct a booking time from free text. ``find_slots``
issues an HMAC-signed token describing an exact provider/type/instant, and ``book``
accepts nothing else. That makes a hallucinated appointment **structurally
unbookable** rather than merely unlikely -- a guarantee no amount of prompting gives.

Tokens are short-lived so a caller cannot book a slot quoted twenty minutes ago
that somebody else has since taken.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from clinic_agent.scheduling.models import Slot

DEFAULT_TTL_SECONDS = 900


class InvalidSlotToken(Exception):
    """Raised when a token is malformed, forged, or expired."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def issue_slot_token(
    slot: Slot,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    payload = {
        "p": slot.provider_id,
        "t": slot.appointment_type_id,
        "s": int(slot.start.timestamp()),
        "e": int(slot.end.timestamp()),
        "x": int(now.timestamp()) + ttl_seconds,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body, secret)}"


def verify_slot_token(token: str, secret: str, *, now: datetime | None = None) -> Slot:
    """Return the Slot a token encodes, or raise InvalidSlotToken."""
    now = now or datetime.now(UTC)

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidSlotToken("malformed token")
    body, signature = parts

    # compare_digest, never ==, so a forged token cannot be found byte-by-byte
    # by timing repeated attempts.
    if not hmac.compare_digest(signature, _sign(body, secret)):
        raise InvalidSlotToken("signature mismatch")

    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:
        raise InvalidSlotToken("undecodable payload") from exc

    if int(now.timestamp()) > payload["x"]:
        raise InvalidSlotToken("token expired")

    return Slot(
        provider_id=payload["p"],
        appointment_type_id=payload["t"],
        start=datetime.fromtimestamp(payload["s"], tz=UTC),
        end=datetime.fromtimestamp(payload["e"], tz=UTC),
    )
