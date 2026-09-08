"""Test doubles for the voice bridge.

Deliberately built from real `google.genai.types` objects rather than mocks, so the
bridge is exercised against the same shapes the SDK actually delivers. No network,
no credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager

from google.genai import types

# --- Telnyx side --------------------------------------------------------------


class FakeTelnyxSocket:
    """Replays a scripted sequence of inbound frames and records what we send.

    `hold_open` keeps the socket alive after the script is exhausted, the way a real
    call stays connected, so a test can observe behaviour driven by the Gemini side.
    """

    def __init__(self, script: list[str], *, hold_open: bool = False):
        self._script = list(script)
        self._hold_open = hold_open
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._script:
            return self._script.pop(0)
        if self._hold_open:
            await asyncio.Event().wait()  # cancelled when the session tears down
        raise StopAsyncIteration

    @property
    def sent_events(self) -> list[str]:
        return [json.loads(s)["event"] for s in self.sent]

    @property
    def media_frames(self) -> list[bytes]:
        return [
            base64.b64decode(json.loads(s)["media"]["payload"])
            for s in self.sent
            if json.loads(s)["event"] == "media"
        ]

    @property
    def sent_audio(self) -> bytes:
        return b"".join(self.media_frames)


def telnyx_start(call_control_id: str = "ccid-1", from_number: str = "+15550111") -> str:
    return json.dumps({
        "event": "start",
        "stream_id": "stream-1",
        "start": {
            "call_control_id": call_control_id,
            "call_session_id": "csid-1",
            "from": from_number,
            "to": "+15550222",
            "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
        },
    })


def telnyx_media(audio: bytes) -> str:
    return json.dumps({
        "event": "media",
        "stream_id": "stream-1",
        "media": {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "20",
            "payload": base64.b64encode(audio).decode(),
        },
    })


def telnyx_stop(call_control_id: str = "ccid-1") -> str:
    return json.dumps({
        "event": "stop",
        "stream_id": "stream-1",
        "stop": {"call_control_id": call_control_id},
    })


def telnyx_dtmf(digit: str) -> str:
    return json.dumps({"event": "dtmf", "dtmf": {"digit": digit}})


# --- Gemini side --------------------------------------------------------------


class FakeGeminiSession:
    def __init__(self, messages: list[types.LiveServerMessage] | None = None):
        self.messages = list(messages or [])
        self.audio_in: list[types.Blob] = []
        self.tool_responses: list[types.FunctionResponse] = []
        self.text_in: list[str] = []

    async def send_realtime_input(self, *, audio=None, text=None, **_kw) -> None:
        if audio is not None:
            self.audio_in.append(audio)
        if text is not None:
            self.text_in.append(text)

    async def send_tool_response(self, *, function_responses) -> None:
        if isinstance(function_responses, list | tuple):
            self.tool_responses.extend(function_responses)
        else:
            self.tool_responses.append(function_responses)

    async def receive(self):
        for message in self.messages:
            yield message
            await asyncio.sleep(0)  # let other tasks run, as a real socket would


class FakeConnector:
    """Hands out scripted sessions and records the resumption handle used each time."""

    def __init__(self, *sessions: FakeGeminiSession):
        self.sessions = list(sessions)
        self.handles: list[str | None] = []

    def __call__(self, resumption_handle: str | None):
        self.handles.append(resumption_handle)
        if not self.sessions:
            raise AssertionError("connector called more times than sessions provided")
        session = self.sessions.pop(0)

        @asynccontextmanager
        async def _cm():
            yield session

        return _cm()


def gemini_audio(pcm_little_endian_24k: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="audio/pcm;rate=24000", data=pcm_little_endian_24k
                        )
                    )
                ]
            )
        )
    )


def gemini_interrupted() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))


def gemini_tool_call(name: str, args: dict, call_id: str = "fc-1") -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id=call_id, name=name, args=args)]
        )
    )


def gemini_resumption(handle: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle=handle, resumable=True
        )
    )


def gemini_go_away() -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway())


def gemini_output_transcript(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text=text)
        )
    )


def gemini_input_transcript(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text)
        )
    )
