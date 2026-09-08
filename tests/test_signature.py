"""Telnyx webhook signature verification.

Without this, anyone who learns the webhook URL can make the clinic's phone system
answer calls, transfer them, or hang up. Telnyx signs with Ed25519 over
`{timestamp}|{raw_body}`.
"""

from __future__ import annotations

import base64
import time

import pytest
from nacl.signing import SigningKey

from clinic_agent.telephony.signature import (
    InvalidWebhookSignature,
    verify_webhook,
)

BODY = b'{"data":{"event_type":"call.initiated"}}'


def make_keypair():
    signing_key = SigningKey.generate()
    public_key_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode()
    return signing_key, public_key_b64


def sign(signing_key, body: bytes, timestamp: str) -> str:
    signed = signing_key.sign(f"{timestamp}|".encode() + body)
    return base64.b64encode(signed.signature).decode()


def test_a_genuine_signature_verifies():
    key, public = make_keypair()
    ts = str(int(time.time()))
    verify_webhook(body=BODY, signature_b64=sign(key, BODY, ts), timestamp=ts, public_key_b64=public)


def test_tampered_body_is_rejected():
    key, public = make_keypair()
    ts = str(int(time.time()))
    signature = sign(key, BODY, ts)
    with pytest.raises(InvalidWebhookSignature, match="signature"):
        verify_webhook(
            body=b'{"data":{"event_type":"call.hangup"}}',
            signature_b64=signature, timestamp=ts, public_key_b64=public,
        )


def test_tampered_timestamp_is_rejected():
    key, public = make_keypair()
    ts = str(int(time.time()))
    signature = sign(key, BODY, ts)
    with pytest.raises(InvalidWebhookSignature, match="signature"):
        verify_webhook(
            body=BODY, signature_b64=signature,
            timestamp=str(int(ts) + 1), public_key_b64=public,
        )


def test_a_stale_timestamp_is_rejected_even_with_a_valid_signature():
    """Replay protection. A captured webhook must not work an hour later."""
    key, public = make_keypair()
    old = str(int(time.time()) - 3600)
    with pytest.raises(InvalidWebhookSignature, match="too old"):
        verify_webhook(
            body=BODY, signature_b64=sign(key, BODY, old),
            timestamp=old, public_key_b64=public,
        )


def test_a_timestamp_inside_the_tolerance_is_accepted():
    key, public = make_keypair()
    recent = str(int(time.time()) - 60)
    verify_webhook(
        body=BODY, signature_b64=sign(key, BODY, recent),
        timestamp=recent, public_key_b64=public,
    )


def test_a_future_timestamp_beyond_tolerance_is_rejected():
    """Clock skew is bounded; a far-future timestamp is not legitimate."""
    key, public = make_keypair()
    future = str(int(time.time()) + 3600)
    with pytest.raises(InvalidWebhookSignature, match="too old|skew"):
        verify_webhook(
            body=BODY, signature_b64=sign(key, BODY, future),
            timestamp=future, public_key_b64=public,
        )


def test_signature_from_a_different_key_is_rejected():
    attacker, _ = make_keypair()
    _, real_public = make_keypair()
    ts = str(int(time.time()))
    with pytest.raises(InvalidWebhookSignature, match="signature"):
        verify_webhook(
            body=BODY, signature_b64=sign(attacker, BODY, ts),
            timestamp=ts, public_key_b64=real_public,
        )


@pytest.mark.parametrize("bad_sig", ["", "not-base64!!", base64.b64encode(b"short").decode()])
def test_malformed_signatures_are_rejected(bad_sig):
    _, public = make_keypair()
    ts = str(int(time.time()))
    with pytest.raises(InvalidWebhookSignature):
        verify_webhook(body=BODY, signature_b64=bad_sig, timestamp=ts, public_key_b64=public)


def test_malformed_public_key_is_rejected():
    key, _ = make_keypair()
    ts = str(int(time.time()))
    with pytest.raises(InvalidWebhookSignature, match="public key"):
        verify_webhook(
            body=BODY, signature_b64=sign(key, BODY, ts),
            timestamp=ts, public_key_b64="way-too-short",
        )


def test_non_numeric_timestamp_is_rejected():
    key, public = make_keypair()
    with pytest.raises(InvalidWebhookSignature, match="timestamp"):
        verify_webhook(
            body=BODY, signature_b64=sign(key, BODY, "abc"),
            timestamp="abc", public_key_b64=public,
        )
