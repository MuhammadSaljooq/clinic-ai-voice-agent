"""Operator dashboard: authentication, rendering, and read-only behaviour."""

from __future__ import annotations

import base64
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from clinic_agent.config import load_config
from clinic_agent.db.calls import record_call
from clinic_agent.db.seed import seed_from_config
from clinic_agent.web.dashboard import build_dashboard

REPO = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "dashboard-test-password"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"clinic:{PASSWORD}".encode()).decode()}
NOW = datetime.now(UTC)


class FakeOutcome:
    def __init__(self, **kw):
        self.call_control_id = kw.get("call_control_id", "ccid-1")
        self.from_number = kw.get("from_number", "+15550100")
        self.ended_reason = kw.get("ended_reason", "telnyx_stop")
        self.transcript = kw.get("transcript", [])


@pytest.fixture
async def dash(pool):
    """httpx + ASGITransport, not TestClient.

    TestClient drives the app from a separate event loop via anyio's blocking portal,
    but asyncpg connections are bound to the loop that created them -- so any query
    fails with "attached to a different loop". ASGITransport runs the app in this
    test's own loop.
    """
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    app = FastAPI()
    app.include_router(build_dashboard(cfg, lambda: pool, PASSWORD))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://dash") as client:
        yield client, pool, cfg


# --- authentication ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/appointments", "/dashboard/reminders"])
async def test_every_page_requires_a_password(dash, path):
    client, _pool, _cfg = dash
    assert (await client.get(path)).status_code == 401


async def test_a_wrong_password_is_rejected(dash):
    client, _pool, _cfg = dash
    bad = {"Authorization": "Basic " + base64.b64encode(b"clinic:wrong").decode()}
    assert (await client.get("/dashboard", headers=bad)).status_code == 401


# --- rendering -----------------------------------------------------------------


async def test_calls_page_renders_when_empty(dash):
    client, _pool, cfg = dash
    response = await client.get("/dashboard", headers=AUTH)
    assert response.status_code == 200
    assert cfg.clinic.name in response.text
    assert "No calls yet." in response.text


async def test_a_recorded_call_appears_with_its_transcript(dash):
    """The whole point of the dashboard: seeing what the bot actually said."""
    client, pool, _cfg = dash
    await record_call(pool, FakeOutcome(transcript=[
        {"role": "agent", "text": "Thanks for calling, this is an AI assistant."},
        {"role": "caller", "text": "I need a follow-up"},
    ]))

    body = (await client.get("/dashboard", headers=AUTH)).text

    assert "+15550100" in body
    assert "this is an AI assistant" in body
    assert "I need a follow-up" in body
    assert "telnyx_stop" in body


async def test_transcript_content_is_html_escaped(dash):
    """A caller could say something that looks like markup; it must not render."""
    client, pool, _cfg = dash
    await record_call(pool, FakeOutcome(transcript=[
        {"role": "caller", "text": "<script>alert('x')</script>"}
    ]))
    body = (await client.get("/dashboard", headers=AUTH)).text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


async def test_appointments_page_lists_upcoming_bookings(dash):
    client, pool, _cfg = dash
    patient = await pool.fetchval(
        "INSERT INTO patients (name, phone) VALUES ('Ada Lovelace','+15550100') RETURNING id"
    )
    starts = NOW + timedelta(days=1)
    await pool.execute(
        "INSERT INTO appointments (provider_id, appointment_type_id, patient_id,"
        " starts_at, ends_at, blocked_range) VALUES (1,1,$1,$2,$3,tstzrange($2,$3))",
        patient, starts, starts + timedelta(minutes=15),
    )

    body = (await client.get("/dashboard/appointments", headers=AUTH)).text

    assert "Ada Lovelace" in body
    assert "Dr. Reyes" in body
    assert "Follow-up" in body


async def test_reminders_page_shows_the_recorded_body(dash):
    """In dry-run this is how the wording gets reviewed before anyone is texted."""
    client, pool, _cfg = dash
    patient = await pool.fetchval(
        "INSERT INTO patients (name, phone) VALUES ('Ada','+15550100') RETURNING id"
    )
    starts = NOW + timedelta(hours=20)
    appointment = await pool.fetchval(
        "INSERT INTO appointments (provider_id, appointment_type_id, patient_id,"
        " starts_at, ends_at, blocked_range) VALUES (1,1,$1,$2,$3,tstzrange($2,$3))"
        " RETURNING id",
        patient, starts, starts + timedelta(minutes=15),
    )
    await pool.execute(
        "INSERT INTO reminders (appointment_id, scheduled_for, status, body, attempts)"
        " VALUES ($1, now(), 'dry_run', 'Northside: reminder of your appointment tomorrow at 9am.', 1)",
        appointment,
    )

    body = (await client.get("/dashboard/reminders", headers=AUTH)).text

    assert "dry_run" in body
    assert "reminder of your appointment tomorrow at 9am" in body


async def test_the_dry_run_banner_warns_that_nothing_is_being_sent(dash):
    client, _pool, cfg = dash
    cfg.reminders.enabled = True
    cfg.reminders.dry_run = True
    body = (await client.get("/dashboard", headers=AUTH)).text
    assert "dry-run" in body


async def test_a_disabled_reminder_config_is_called_out(dash):
    client, _pool, cfg = dash
    assert cfg.reminders.enabled is False
    body = (await client.get("/dashboard", headers=AUTH)).text
    assert "Reminders are switched off" in body


# --- read only -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/appointments", "/dashboard/reminders"])
async def test_the_dashboard_exposes_no_write_routes(dash, path):
    """A leaked password must not be able to cancel a patient's appointment."""
    client, _pool, _cfg = dash
    for method in ("post", "put", "patch", "delete"):
        response = await getattr(client, method)(path, headers=AUTH)
        assert response.status_code == 405, f"{method.upper()} {path} should not be allowed"


# --- call persistence ----------------------------------------------------------


async def test_recording_the_same_call_twice_does_not_duplicate_it(dash):
    """A reconnect or a retried teardown must not create a second row."""
    _client, pool, _cfg = dash
    await record_call(pool, FakeOutcome(transcript=[{"role": "agent", "text": "first"}]))
    await record_call(pool, FakeOutcome(transcript=[{"role": "agent", "text": "second"}]))

    assert await pool.fetchval("SELECT count(*) FROM calls") == 1
    stored = await pool.fetchval("SELECT transcript::text FROM calls")
    assert "second" in stored, "the later record should win"


async def test_recording_a_call_never_raises(dash):
    """By the time this runs the call is over; bookkeeping must not blow up."""
    _client, pool, _cfg = dash

    class Broken:
        def __init__(self):
            self.call_control_id = object()  # unserialisable
            self.from_number = "+1"
            self.ended_reason = "x"
            self.transcript = []

    assert await record_call(pool, Broken()) is None
