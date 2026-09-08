"""Telnyx media-streaming WebSocket frame codec.

Decoding must be defensive: this is a live phone call, and an unexpected frame
shape should never take the call down. Unknown events are surfaced as data.
"""

from __future__ import annotations

import base64
import json

import pytest

from clinic_agent.telephony.stream_protocol import (
    Connected,
    Dtmf,
    ErrorFrame,
    MalformedFrame,
    MediaIn,
    StreamStart,
    StreamStop,
    UnknownEvent,
    decode_frame,
    encode_clear,
    encode_mark,
    encode_media,
)


def test_decode_connected():
    frame = decode_frame(json.dumps({"event": "connected", "version": "1.0.0"}))
    assert frame == Connected(version="1.0.0")


def test_decode_start_exposes_call_identity_and_media_format():
    raw = json.dumps({
        "event": "start",
        "sequence_number": "1",
        "stream_id": "stream-abc",
        "start": {
            "call_control_id": "ccid-123",
            "call_session_id": "csid-456",
            "from": "+15550111",
            "to": "+15550222",
            "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
        },
    })
    frame = decode_frame(raw)
    assert isinstance(frame, StreamStart)
    assert frame.call_control_id == "ccid-123"
    assert frame.from_number == "+15550111"
    assert frame.encoding == "L16"
    assert frame.sample_rate == 16000
    assert frame.stream_id == "stream-abc"


def test_decode_media_returns_decoded_audio_bytes():
    audio = b"\x01\x02\x03\x04"
    raw = json.dumps({
        "event": "media",
        "stream_id": "s1",
        "media": {
            "track": "inbound",
            "chunk": "7",
            "timestamp": "140",
            "payload": base64.b64encode(audio).decode(),
        },
    })
    frame = decode_frame(raw)
    assert isinstance(frame, MediaIn)
    assert frame.audio == audio
    assert frame.track == "inbound"


def test_decode_media_with_undecodable_payload_is_malformed():
    raw = json.dumps({"event": "media", "media": {"payload": "!!!not base64!!!"}})
    with pytest.raises(MalformedFrame):
        decode_frame(raw)


def test_decode_stop():
    raw = json.dumps({"event": "stop", "stream_id": "s1", "stop": {"call_control_id": "ccid-9"}})
    frame = decode_frame(raw)
    assert isinstance(frame, StreamStop)
    assert frame.call_control_id == "ccid-9"


def test_decode_dtmf():
    """DTMF is handled outside the model so pressing 0 always reaches a human."""
    frame = decode_frame(json.dumps({"event": "dtmf", "dtmf": {"digit": "0"}}))
    assert frame == Dtmf(digit="0")


def test_decode_error_frame():
    raw = json.dumps({
        "event": "error",
        "code": 100002,
        "title": "rate limit",
        "detail": "too many frames",
    })
    frame = decode_frame(raw)
    assert isinstance(frame, ErrorFrame)
    assert frame.code == 100002
    assert "rate limit" in frame.title


def test_unknown_event_is_surfaced_not_raised():
    """Telnyx may add events; an unrecognised one must not drop the call."""
    frame = decode_frame(json.dumps({"event": "something_new", "foo": 1}))
    assert isinstance(frame, UnknownEvent)
    assert frame.event == "something_new"
    assert frame.raw["foo"] == 1


def test_missing_event_key_is_malformed():
    with pytest.raises(MalformedFrame, match="event"):
        decode_frame(json.dumps({"no_event_here": True}))


def test_invalid_json_is_malformed():
    with pytest.raises(MalformedFrame):
        decode_frame("{not json")


def test_encode_media_base64s_the_payload():
    encoded = json.loads(encode_media(b"\xde\xad\xbe\xef"))
    assert encoded["event"] == "media"
    assert base64.b64decode(encoded["media"]["payload"]) == b"\xde\xad\xbe\xef"


def test_encode_clear_is_exactly_the_documented_shape():
    """This frame is what makes barge-in feel instant; shape must be exact."""
    assert json.loads(encode_clear()) == {"event": "clear"}


def test_encode_mark_carries_a_name():
    encoded = json.loads(encode_mark("greeting-done"))
    assert encoded["event"] == "mark"
    assert encoded["mark"]["name"] == "greeting-done"
