"""The browser 'Test agent' console.

The page + auth gate are checked over the real HTTP surface; the audio+transcript
bridge is checked at the CallSession level (deterministic, no WebSocket timing), since
that is exactly what the /dashboard/testcall endpoint wires up.
"""

from __future__ import annotations

import contextlib
import pathlib

import numpy as np
import pytest
from fakes import (
    FakeConnector,
    FakeGeminiSession,
    FakeTelnyxSocket,
    gemini_audio,
    gemini_output_transcript,
    telnyx_media,
    telnyx_start,
)
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from clinic_agent.bridge.call_session import CallSession
from clinic_agent.config import load_config
from clinic_agent.web.auth import build_auth_router
from clinic_agent.web.dashboard import build_dashboard

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config.yaml")
PASSWORD = "testcall-pw"
TONE = (np.sin(np.arange(2400) / 8) * 8000).astype("<i2").tobytes()  # 100ms @ 24kHz


def _app(*, connect_gemini):
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PASSWORD))
    app.include_router(build_dashboard(
        CFG, lambda: None, PASSWORD, connect_gemini=connect_gemini, tool_handler_getter=lambda: None,
    ))
    return app


# --- HTTP surface --------------------------------------------------------------


def test_the_test_page_renders_a_talk_button():
    client = TestClient(_app(connect_gemini=FakeConnector()))
    client.post("/login", data={"password": PASSWORD})
    body = client.get("/dashboard/test").text
    assert "talk-btn" in body
    assert "/dashboard/testcall" in body


def test_the_test_page_degrades_when_the_agent_is_not_configured():
    client = TestClient(_app(connect_gemini=None))
    client.post("/login", data={"password": PASSWORD})
    body = client.get("/dashboard/test").text
    assert "not configured" in body


def test_testcall_requires_a_session():
    """Otherwise it would be an open, Gemini-billed voice endpoint."""
    client = TestClient(_app(connect_gemini=FakeConnector()))  # not logged in
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/dashboard/testcall") as ws:
        ws.receive_text()


# --- the bridge the endpoint wires up ------------------------------------------


async def test_transcript_is_streamed_and_agent_audio_is_bridged_back():
    """/dashboard/testcall runs exactly this: browser frames in, agent audio out, and
    an on_transcript sink for the live conversation view."""
    socket = FakeTelnyxSocket([telnyx_start(call_control_id="bt"), telnyx_media(b"\x00\x00" * 160)])
    session = FakeGeminiSession(turns=[[
        gemini_output_transcript("Hi, this is an AI assistant."),
        gemini_audio(TONE),
    ]])
    lines: list[tuple[str, str]] = []

    async def sink(role: str, text: str) -> None:
        lines.append((role, text))

    call = CallSession(telnyx=socket, connect_gemini=FakeConnector(session), on_transcript=sink)
    with contextlib.suppress(Exception):
        await call.run()

    assert any(role == "agent" and "AI assistant" in text for role, text in lines), (
        "the agent's words never reached the transcript sink"
    )
    assert "media" in socket.sent_events, "no agent audio was bridged back to the browser"
