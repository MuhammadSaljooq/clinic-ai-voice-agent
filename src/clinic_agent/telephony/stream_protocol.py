"""Codec for Telnyx media-streaming WebSocket frames.

Pure functions over strings and bytes: no sockets, no state. Decoding is deliberately
defensive because this runs during a live phone call -- an event shape we have not seen
before must never take the call down, so unknown events come back as data rather than
exceptions. Only genuinely unusable input (bad JSON, undecodable audio, no event key)
raises.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any


class MalformedFrame(Exception):
    """The frame could not be interpreted at all."""


@dataclass(frozen=True, slots=True)
class Connected:
    version: str


@dataclass(frozen=True, slots=True)
class StreamStart:
    call_control_id: str
    call_session_id: str
    stream_id: str
    from_number: str
    to_number: str
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class MediaIn:
    audio: bytes
    track: str
    chunk: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class StreamStop:
    call_control_id: str
    stream_id: str


@dataclass(frozen=True, slots=True)
class Dtmf:
    digit: str


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    code: int | None
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    event: str
    raw: dict[str, Any] = field(default_factory=dict)


InboundFrame = (
    Connected | StreamStart | MediaIn | StreamStop | Dtmf | ErrorFrame | UnknownEvent
)


def decode_frame(text: str | bytes) -> InboundFrame:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedFrame(f"not valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "event" not in payload:
        raise MalformedFrame("frame has no 'event' key")

    event = payload["event"]

    if event == "connected":
        return Connected(version=str(payload.get("version", "")))

    if event == "start":
        start = payload.get("start") or {}
        media_format = start.get("media_format") or {}
        return StreamStart(
            call_control_id=str(start.get("call_control_id", "")),
            call_session_id=str(start.get("call_session_id", "")),
            stream_id=str(payload.get("stream_id", "")),
            from_number=str(start.get("from", "")),
            to_number=str(start.get("to", "")),
            encoding=str(media_format.get("encoding", "")),
            sample_rate=int(media_format.get("sample_rate", 0) or 0),
            channels=int(media_format.get("channels", 1) or 1),
        )

    if event == "media":
        media = payload.get("media") or {}
        raw_payload = media.get("payload", "")
        try:
            audio = base64.b64decode(raw_payload, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise MalformedFrame(f"media payload is not valid base64: {exc}") from exc
        return MediaIn(
            audio=audio,
            track=str(media.get("track", "")),
            chunk=str(media.get("chunk", "")),
            timestamp=str(media.get("timestamp", "")),
        )

    if event == "stop":
        stop = payload.get("stop") or {}
        return StreamStop(
            call_control_id=str(stop.get("call_control_id", "")),
            stream_id=str(payload.get("stream_id", "")),
        )

    if event == "dtmf":
        dtmf = payload.get("dtmf") or {}
        return Dtmf(digit=str(dtmf.get("digit", "")))

    if event == "error":
        # Telnyx has sent these both flat and nested; accept either.
        body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        code = body.get("code")
        return ErrorFrame(
            code=int(code) if isinstance(code, int | str) and str(code).isdigit() else None,
            title=str(body.get("title", "")),
            detail=str(body.get("detail", "")),
        )

    return UnknownEvent(event=str(event), raw=payload)


def encode_media(pcm: bytes) -> str:
    """Frame outbound audio for Telnyx. Caller supplies big-endian L16 at 16 kHz."""
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(pcm).decode()}}
    )


def encode_clear() -> str:
    """Drop audio Telnyx has buffered but not yet played.

    This is what makes barge-in feel instant. Clearing only our own local queue still
    leaves several hundred milliseconds of speech in flight, which is the single most
    robot-like thing a voice agent can do.
    """
    return json.dumps({"event": "clear"})


def encode_mark(name: str) -> str:
    """Ask Telnyx to echo a marker back once preceding audio has finished playing."""
    return json.dumps({"event": "mark", "mark": {"name": name}})
