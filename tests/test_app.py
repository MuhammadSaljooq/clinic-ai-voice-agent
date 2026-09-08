"""FastAPI surface: webhook authentication and the media-streaming WebSocket."""

from __future__ import annotations

import base64
import contextlib
import json
import pathlib
import time

import numpy as np
from fakes import FakeConnector, FakeGeminiSession, gemini_audio, telnyx_start
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from clinic_agent.app import AppDeps, TelnyxWebSocketAdapter, create_app
from clinic_agent.audio.codec import FRAME_BYTES
from clinic_agent.config import load_config
from clinic_agent.telephony.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config.yaml")
STREAM_URL = "wss://example.test/telnyx/stream"


class StubTelnyx:
    def __init__(self):
        self.answered: list[tuple[str, str]] = []

    async def answer_with_stream(self, call_control_id, *, stream_url, codec="L16"):
        self.answered.append((call_control_id, stream_url))
        return {}


def build(*, gemini_sessions=(), on_finished=None):
    key = SigningKey.generate()
    public = base64.b64encode(bytes(key.verify_key)).decode()
    telnyx = StubTelnyx()
    finished: list = []

    async def record(outcome):
        finished.append(outcome)

    deps = AppDeps(
        cfg=CFG,
        telnyx=telnyx,
        public_key_b64=public,
        stream_url=STREAM_URL,
        connect_gemini=FakeConnector(*gemini_sessions),
        tool_handler=None,
        on_call_finished=on_finished or record,
    )
    return TestClient(create_app(deps)), key, telnyx, finished


def signed_headers(key, body: bytes, *, age_seconds: int = 0):
    ts = str(int(time.time()) - age_seconds)
    signature = key.sign(f"{ts}|".encode() + body).signature
    return {
        SIGNATURE_HEADER: base64.b64encode(signature).decode(),
        TIMESTAMP_HEADER: ts,
        "content-type": "application/json",
    }


CALL_INITIATED = json.dumps(
    {"data": {"event_type": "call.initiated", "payload": {"call_control_id": "ccid-42"}}}
).encode()


def test_health_reports_the_configured_clinic():
    client, *_ = build()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["clinic"] == CFG.clinic.name


def test_an_unsigned_webhook_is_rejected():
    """Without this the webhook URL is an open control channel for the phone line."""
    client, _key, telnyx, _ = build()
    response = client.post("/telnyx/webhook", content=CALL_INITIATED)
    assert response.status_code == 401
    assert telnyx.answered == [], "must not act on an unverified webhook"


def test_a_forged_signature_is_rejected():
    client, _key, telnyx, _ = build()
    attacker = SigningKey.generate()
    response = client.post(
        "/telnyx/webhook", content=CALL_INITIATED, headers=signed_headers(attacker, CALL_INITIATED)
    )
    assert response.status_code == 401
    assert telnyx.answered == []


def test_a_replayed_webhook_is_rejected():
    client, key, telnyx, _ = build()
    headers = signed_headers(key, CALL_INITIATED, age_seconds=3600)
    response = client.post("/telnyx/webhook", content=CALL_INITIATED, headers=headers)
    assert response.status_code == 401
    assert telnyx.answered == []


def test_a_signed_call_initiated_answers_with_the_stream_url():
    client, key, telnyx, _ = build()
    response = client.post(
        "/telnyx/webhook", content=CALL_INITIATED, headers=signed_headers(key, CALL_INITIATED)
    )
    assert response.status_code == 200
    assert telnyx.answered == [("ccid-42", STREAM_URL)]


def test_other_events_are_acknowledged_without_answering():
    client, key, telnyx, _ = build()
    body = json.dumps({"data": {"event_type": "call.hangup", "payload": {}}}).encode()
    response = client.post("/telnyx/webhook", content=body, headers=signed_headers(key, body))
    assert response.status_code == 200
    assert telnyx.answered == []


def test_the_stream_websocket_carries_audio_both_ways():
    """End-to-end through the real FastAPI WebSocket route with a fake Gemini.

    Waiting on a media frame rather than on socket close keeps this deterministic:
    the server sends audio as soon as Gemini produces it, so there is nothing to race.
    """
    tone = (np.sin(np.arange(2400) / 8) * 8000).astype("<i2").tobytes()  # 100 ms @ 24 kHz
    client, *_ = build(gemini_sessions=[FakeGeminiSession([gemini_audio(tone)])])

    received: list[str] = []
    with contextlib.suppress(Exception), client.websocket_connect("/telnyx/stream") as ws:
        ws.send_text(telnyx_start(call_control_id="ccid-ws"))
        received.append(ws.receive_text())

    assert received, "no frame came back from the bridge"
    frame = json.loads(received[0])
    assert frame["event"] == "media"
    assert len(base64.b64decode(frame["media"]["payload"])) == FRAME_BYTES


# --- the WebSocket adapter ----------------------------------------------------


class StubWebSocket:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        if not self._incoming:
            raise WebSocketDisconnect(code=1000)
        return self._incoming.pop(0)


async def test_adapter_forwards_sends_to_the_websocket():
    stub = StubWebSocket([])
    await TelnyxWebSocketAdapter(stub).send("hello")
    assert stub.sent == ["hello"]


async def test_adapter_iterates_incoming_frames():
    adapter = TelnyxWebSocketAdapter(StubWebSocket(["a", "b"]))
    assert [frame async for frame in adapter] == ["a", "b"]


async def test_adapter_turns_a_disconnect_into_a_clean_end_of_iteration():
    """A hangup must read as end-of-stream, not as an exception mid-call."""
    adapter = TelnyxWebSocketAdapter(StubWebSocket([]))
    assert [frame async for frame in adapter] == []


# --- inbound SMS webhook -------------------------------------------------------


def test_an_unsigned_messaging_webhook_is_rejected():
    """Without this, anyone could opt patients out or cancel their appointments."""
    client, *_ = build()
    body = json.dumps({"data": {"event_type": "message.received"}}).encode()
    assert client.post("/telnyx/messaging", content=body).status_code == 401


def test_a_signed_messaging_webhook_without_a_pool_does_not_crash():
    client, key, *_ = build()
    body = json.dumps({
        "data": {
            "event_type": "message.received",
            "payload": {"from": {"phone_number": "+15550100"}, "text": "STOP"},
        }
    }).encode()
    response = client.post("/telnyx/messaging", content=body, headers=signed_headers(key, body))
    assert response.status_code == 200
    assert response.text == "not configured"


def test_non_message_events_are_acknowledged_and_ignored():
    client, key, *_ = build()
    body = json.dumps({"data": {"event_type": "message.sent", "payload": {}}}).encode()
    response = client.post("/telnyx/messaging", content=body, headers=signed_headers(key, body))
    assert response.status_code == 200
    assert response.text == "ignored"
