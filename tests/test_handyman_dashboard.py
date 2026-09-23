"""The handyman console: four pages, the mark/status actions, auth, and call recording."""

from __future__ import annotations

import pathlib

import pytest
from fakes import FakeConnector
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from clinic_agent.db.handyman_calls import record_handyman_call
from clinic_agent.handyman_config import load_handyman_config
from clinic_agent.web.auth import build_auth_router
from clinic_agent.web.handyman_dashboard import build_handyman_dashboard

REPO = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "handyman-pw"
FORM = {"content-type": "application/x-www-form-urlencoded"}


class FakeOutcome:
    def __init__(self, **kw):
        self.call_control_id = kw.get("call_control_id")
        self.from_number = kw.get("from_number")
        self.ended_reason = kw.get("ended_reason", "hangup")
        self.transcript = kw.get("transcript", [])


@pytest.fixture
async def handy(pool):
    cfg = load_handyman_config(REPO / "handyman_config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_handyman_dashboard(
        cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector(), tool_handler_getter=lambda: None,
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/login", content=f"password={PASSWORD}", headers=FORM)
        yield client, pool, cfg


# --- auth ----------------------------------------------------------------------


async def test_pages_require_a_session(pool):
    cfg = load_handyman_config(REPO / "handyman_config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_handyman_dashboard(cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard/handyman/appointments")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_voicecall_ws_requires_a_session(pool):
    cfg = load_handyman_config(REPO / "handyman_config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_handyman_dashboard(cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector()))
    client = TestClient(app)  # not logged in
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/dashboard/handyman/voicecall") as ws:
        ws.receive_text()


# --- voice assistant -----------------------------------------------------------


async def test_voice_page_renders_the_mic_console(handy):
    client, _pool, _cfg = handy
    body = (await client.get("/dashboard/handyman/voice")).text
    assert "talk-btn" in body
    assert "/dashboard/handyman/voicecall" in body


# --- appointments --------------------------------------------------------------


async def test_appointments_empty_state(handy):
    client, _pool, _cfg = handy
    body = (await client.get("/dashboard/handyman/appointments")).text
    assert "No requests yet" in body


async def test_appointments_lists_a_request_and_status_can_change(handy):
    client, pool, _cfg = handy
    rid = await pool.fetchval(
        "INSERT INTO handyman_appointment_requests (name, phone, job_type, address, preferred_time)"
        " VALUES ('Ada Lovelace','+18655550100','drywall','West Knoxville','Thursday AM') RETURNING id"
    )
    body = (await client.get("/dashboard/handyman/appointments")).text
    assert "Ada Lovelace" in body
    assert "drywall" in body
    assert "requested" in body

    r = await client.post(
        f"/dashboard/handyman/appointments/{rid}/status", content="status=confirmed", headers=FORM
    )
    assert r.status_code == 303
    status = await pool.fetchval(
        "SELECT status::text FROM handyman_appointment_requests WHERE id=$1", rid
    )
    assert status == "confirmed"


async def test_a_bogus_status_is_ignored(handy):
    client, pool, _cfg = handy
    rid = await pool.fetchval(
        "INSERT INTO handyman_appointment_requests (phone) VALUES ('+18655550101') RETURNING id"
    )
    await client.post(
        f"/dashboard/handyman/appointments/{rid}/status", content="status=evil", headers=FORM
    )
    status = await pool.fetchval(
        "SELECT status::text FROM handyman_appointment_requests WHERE id=$1", rid
    )
    assert status == "requested"  # unchanged


# --- transcriptions ------------------------------------------------------------


async def test_transcriptions_empty_state(handy):
    client, _pool, _cfg = handy
    body = (await client.get("/dashboard/handyman/transcriptions")).text
    assert "No calls yet" in body


async def test_a_recorded_call_appears_with_its_transcript(handy):
    client, pool, _cfg = handy
    await record_handyman_call(
        pool,
        FakeOutcome(transcript=[
            {"role": "agent", "text": "Thanks for calling Task Titan."},
            {"role": "caller", "text": "I need a fence fixed."},
        ]),
        source="console",
    )
    body = (await client.get("/dashboard/handyman/transcriptions")).text
    assert "Task Titan" in body
    assert "fence fixed" in body
    assert "Test console" in body  # console calls have no from-number


# --- leads & messages ----------------------------------------------------------


async def test_leads_and_messages_empty_state(handy):
    client, _pool, _cfg = handy
    body = (await client.get("/dashboard/handyman/leads")).text
    assert "No callback leads" in body
    assert "No messages" in body


async def test_a_lead_appears_and_can_be_marked_contacted(handy):
    client, pool, _cfg = handy
    lid = await pool.fetchval(
        "INSERT INTO handyman_leads (name, phone, reason) VALUES"
        " ('Fox Mulder','+18655552222','deck quote') RETURNING id"
    )
    body = (await client.get("/dashboard/handyman/leads")).text
    assert "Fox Mulder" in body and "deck quote" in body

    r = await client.post(f"/dashboard/handyman/leads/{lid}/contacted")
    assert r.status_code == 303
    assert await pool.fetchval("SELECT status::text FROM handyman_leads WHERE id=$1", lid) == "contacted"


async def test_a_message_appears_and_can_be_marked_read(handy):
    client, pool, _cfg = handy
    mid = await pool.fetchval(
        "INSERT INTO handyman_messages (caller_name, phone, message) VALUES"
        " ('Mom','+18655553333','call about Sunday') RETURNING id"
    )
    body = (await client.get("/dashboard/handyman/leads")).text
    assert "Mom" in body and "Sunday" in body

    r = await client.post(f"/dashboard/handyman/messages/{mid}/read")
    assert r.status_code == 303
    assert await pool.fetchval("SELECT status::text FROM handyman_messages WHERE id=$1", mid) == "read"


# --- call recording ------------------------------------------------------------


async def test_record_handyman_call_inserts_a_console_row(pool):
    got = await record_handyman_call(pool, FakeOutcome(transcript=[{"role": "agent", "text": "hi"}]), source="console")
    assert got is not None
    row = await pool.fetchrow("SELECT source, transcript::text FROM handyman_calls WHERE id=$1", got)
    assert row["source"] == "console"
    assert "hi" in row["transcript"]


async def test_record_handyman_call_upserts_a_phone_call(pool):
    a = await record_handyman_call(pool, FakeOutcome(call_control_id="cc-9", from_number="+1865", transcript=[{"role": "agent", "text": "first"}]))
    b = await record_handyman_call(pool, FakeOutcome(call_control_id="cc-9", from_number="+1865", transcript=[{"role": "agent", "text": "second"}]))
    assert a == b  # same row, upserted on the control id
    count = await pool.fetchval("SELECT count(*) FROM handyman_calls WHERE telnyx_call_control_id='cc-9'")
    assert count == 1
