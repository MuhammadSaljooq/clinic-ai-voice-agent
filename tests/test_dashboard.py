"""Operator dashboard: session auth, rendering, and read-only behaviour."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from clinic_agent.config import load_config
from clinic_agent.db.calls import record_call
from clinic_agent.db.seed import seed_from_config
from clinic_agent.web.auth import build_auth_router
from clinic_agent.web.dashboard import build_dashboard

REPO = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "dashboard-test-password"
NOW = datetime.now(UTC)


class FakeOutcome:
    def __init__(self, **kw):
        self.call_control_id = kw.get("call_control_id", "ccid-1")
        self.from_number = kw.get("from_number", "+15550100")
        self.ended_reason = kw.get("ended_reason", "telnyx_stop")
        self.transcript = kw.get("transcript", [])


class FakeTelnyx:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_sms(self, *, to, from_, text):
        self.sent.append({"to": to, "from": from_, "text": text})
        return {"data": {"id": f"m{len(self.sent)}"}}


async def _login(client: AsyncClient) -> None:
    r = await client.post(
        "/login",
        content=f"password={PASSWORD}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 303


@pytest.fixture
async def dash(pool):
    """Authenticated httpx client over the real app surface (auth + dashboard).

    ASGITransport runs the app in this test's own event loop, so asyncpg connections
    stay on the loop that created them.
    """
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    telnyx = FakeTelnyx()
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(
        build_dashboard(cfg, lambda: pool, PASSWORD, telnyx=telnyx, from_number_getter=lambda: "+15550222")
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://dash") as client:
        await _login(client)
        yield client, pool, cfg, telnyx


PAGES = ["/dashboard", "/dashboard/appointments", "/dashboard/reminders", "/dashboard/inbox"]


# --- authentication ------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
async def test_pages_redirect_to_login_without_a_session(pool, path):
    cfg = load_config(REPO / "config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_dashboard(cfg, lambda: pool, PASSWORD))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://dash") as client:
        r = await client.get(path)  # no cookie
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")


async def test_a_wrong_password_is_rejected(dash):
    client, *_ = dash
    fresh = AsyncClient(transport=client._transport, base_url="http://dash")
    r = await fresh.post(
        "/login", content="password=wrong",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 401
    await fresh.aclose()


async def test_signing_out_clears_the_session(dash):
    client, _pool, _cfg, _t = dash
    out = await client.post("/logout")
    assert out.status_code == 303
    assert out.headers["location"] == "/login"


# --- rendering -----------------------------------------------------------------


async def test_calls_page_renders_when_empty(dash):
    client, _pool, cfg, _t = dash
    response = await client.get("/dashboard")
    assert response.status_code == 200
    assert cfg.clinic.name in response.text
    assert "No calls yet" in response.text


async def test_a_recorded_call_appears_with_its_transcript(dash):
    client, pool, _cfg, _t = dash
    await record_call(pool, FakeOutcome(transcript=[
        {"role": "agent", "text": "Thanks for calling, this is an AI assistant."},
        {"role": "caller", "text": "I need a follow-up"},
    ]))
    body = (await client.get("/dashboard")).text
    assert "+15550100" in body
    assert "this is an AI assistant" in body
    assert "I need a follow-up" in body
    assert "telnyx stop" in body  # underscores are humanised in the status badge


async def test_transcript_content_is_html_escaped(dash):
    client, pool, _cfg, _t = dash
    await record_call(pool, FakeOutcome(transcript=[
        {"role": "caller", "text": "<script>alert('x')</script>"}
    ]))
    body = (await client.get("/dashboard")).text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


async def test_appointments_page_lists_upcoming_bookings(dash):
    client, pool, _cfg, _t = dash
    patient = await pool.fetchval(
        "INSERT INTO patients (name, phone) VALUES ('Ada Lovelace','+15550100') RETURNING id"
    )
    starts = NOW + timedelta(days=1)
    await pool.execute(
        "INSERT INTO appointments (provider_id, appointment_type_id, patient_id,"
        " starts_at, ends_at, blocked_range) VALUES (1,1,$1,$2,$3,tstzrange($2,$3))",
        patient, starts, starts + timedelta(minutes=15),
    )
    body = (await client.get("/dashboard/appointments")).text
    assert "Ada Lovelace" in body
    assert "Dr. Reyes" in body
    assert "Follow-up" in body


async def test_reminders_page_shows_the_recorded_body(dash):
    client, pool, _cfg, _t = dash
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
    body = (await client.get("/dashboard/reminders")).text
    assert "dry run" in body  # underscore rendered as a space in the badge
    assert "reminder of your appointment tomorrow at 9am" in body


async def test_callbacks_page_lists_and_marks_contacted(dash):
    client, pool, _cfg, _t = dash
    cb = await pool.fetchval(
        "INSERT INTO callback_requests (name, phone, reason) VALUES"
        " ('Ada Lovelace','+15550100','soonest follow-up') RETURNING id"
    )
    body = (await client.get("/dashboard/callbacks")).text
    assert "Ada Lovelace" in body
    assert "soonest follow-up" in body
    assert "waiting" in body

    r = await client.post(f"/dashboard/callbacks/{cb}/contacted")
    assert r.status_code == 303
    status = await pool.fetchval("SELECT status::text FROM callback_requests WHERE id=$1", cb)
    assert status == "contacted"


async def test_the_dry_run_banner_warns_that_nothing_is_being_sent(dash):
    client, _pool, cfg, _t = dash
    cfg.reminders.enabled = True
    cfg.reminders.dry_run = True
    body = (await client.get("/dashboard")).text
    assert "dry-run" in body


async def test_a_disabled_reminder_config_is_called_out(dash):
    client, _pool, cfg, _t = dash
    assert cfg.reminders.enabled is False
    body = (await client.get("/dashboard")).text
    assert "Reminders are switched off" in body


# --- read only -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/appointments", "/dashboard/reminders"])
async def test_the_readonly_pages_expose_no_write_routes(dash, path):
    """A leaked session must not be able to cancel a patient's appointment."""
    client, _pool, _cfg, _t = dash
    for method in ("post", "put", "patch", "delete"):
        response = await getattr(client, method)(path)
        assert response.status_code == 405, f"{method.upper()} {path} should not be allowed"


# --- call persistence ----------------------------------------------------------


async def test_recording_the_same_call_twice_does_not_duplicate_it(dash):
    _client, pool, _cfg, _t = dash
    await record_call(pool, FakeOutcome(transcript=[{"role": "agent", "text": "first"}]))
    await record_call(pool, FakeOutcome(transcript=[{"role": "agent", "text": "second"}]))
    assert await pool.fetchval("SELECT count(*) FROM calls") == 1
    stored = await pool.fetchval("SELECT transcript::text FROM calls")
    assert "second" in stored


async def test_recording_a_call_never_raises(dash):
    _client, pool, _cfg, _t = dash

    class Broken:
        def __init__(self):
            self.call_control_id = object()  # unserialisable
            self.from_number = "+1"
            self.ended_reason = "x"
            self.transcript = []

    assert await record_call(pool, Broken()) is None
