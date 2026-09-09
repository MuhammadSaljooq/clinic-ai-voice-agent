"""Signed rental tokens -- the model cannot invent availability or prices.

`find_available_trailers` issues an HMAC-signed token binding one exact unit, date range,
and price; `book_rental` accepts nothing else. So a hallucinated rental is structurally
unbookable. Amounts travel as integer cents to avoid any float drift. Mirrors
`scheduling/tokens.py`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

from clinic_agent.rentals.models import RentalSlot

DEFAULT_TTL_SECONDS = 900


class InvalidRentalToken(Exception):
    """Malformed, forged, or expired token."""


@dataclass(frozen=True, slots=True)
class VerifiedRental:
    slot: RentalSlot
    total_cents: int
    deposit_cents: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def issue_rental_token(
    slot: RentalSlot,
    *,
    total_cents: int,
    deposit_cents: int,
    secret: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    payload = {
        "u": slot.trailer_unit_id,
        "t": slot.trailer_type_id,
        "p": slot.pickup.isoformat(),
        "r": slot.return_.isoformat(),
        "c": total_cents,
        "d": deposit_cents,
        "x": int(now.timestamp()) + ttl_seconds,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body, secret)}"


def verify_rental_token(token: str, secret: str, *, now: datetime | None = None) -> VerifiedRental:
    now = now or datetime.now(UTC)
    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidRentalToken("malformed token")
    body, signature = parts
    if not hmac.compare_digest(signature, _sign(body, secret)):
        raise InvalidRentalToken("signature mismatch")
    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:
        raise InvalidRentalToken("undecodable payload") from exc
    if int(now.timestamp()) > payload["x"]:
        raise InvalidRentalToken("token expired")
    return VerifiedRental(
        slot=RentalSlot(
            trailer_unit_id=payload["u"],
            trailer_type_id=payload["t"],
            pickup=date.fromisoformat(payload["p"]),
            return_=date.fromisoformat(payload["r"]),
        ),
        total_cents=payload["c"],
        deposit_cents=payload["d"],
    )
