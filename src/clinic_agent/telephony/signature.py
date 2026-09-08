"""Telnyx webhook signature verification (Ed25519).

Without this the webhook URL is an open control channel for the clinic's phone
system: anyone who learns it could answer, transfer, or hang up calls.

Telnyx signs `{timestamp}|{raw_body}` with Ed25519 and sends the signature in
`telnyx-signature-ed25519` with the timestamp in `telnyx-timestamp`. The timestamp
must be checked as well as the signature, or a captured webhook can be replayed
forever.
"""

from __future__ import annotations

import base64
import binascii
import time

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

SIGNATURE_HEADER = "telnyx-signature-ed25519"
TIMESTAMP_HEADER = "telnyx-timestamp"
DEFAULT_TOLERANCE_SECONDS = 300

_ED25519_PUBLIC_KEY_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64


class InvalidWebhookSignature(Exception):
    """The webhook did not come from Telnyx, or is being replayed."""


def verify_webhook(
    *,
    body: bytes,
    signature_b64: str,
    timestamp: str,
    public_key_b64: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Raise InvalidWebhookSignature unless the webhook is genuine and fresh."""
    now = time.time() if now is None else now

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise InvalidWebhookSignature(f"timestamp is not an integer: {timestamp!r}") from exc

    # Bounded in both directions: a stale timestamp is a replay, and a far-future one
    # is not explainable by clock skew.
    if abs(now - sent_at) > tolerance_seconds:
        raise InvalidWebhookSignature(
            f"timestamp too old or skewed: {abs(now - sent_at):.0f}s outside "
            f"{tolerance_seconds}s tolerance"
        )

    try:
        key_bytes = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidWebhookSignature(f"public key is not valid base64: {exc}") from exc
    if len(key_bytes) != _ED25519_PUBLIC_KEY_BYTES:
        raise InvalidWebhookSignature(
            f"public key must be {_ED25519_PUBLIC_KEY_BYTES} bytes, got {len(key_bytes)}"
        )

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidWebhookSignature(f"signature is not valid base64: {exc}") from exc
    if len(signature) != _ED25519_SIGNATURE_BYTES:
        raise InvalidWebhookSignature(
            f"signature must be {_ED25519_SIGNATURE_BYTES} bytes, got {len(signature)}"
        )

    message = f"{timestamp}|".encode() + body
    try:
        VerifyKey(key_bytes).verify(message, signature)
    except BadSignatureError as exc:
        raise InvalidWebhookSignature("signature does not match body and timestamp") from exc
