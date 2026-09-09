"""The Trailer-rental console section: pages, auth, and the test-call gate."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeConnector
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from clinic_agent.db.rental_seed import seed_rentals_from_config
from clinic_agent.rental_config import load_rental_config
from clinic_agent.web.auth import build_auth_router
from clinic_agent.web.rental_dashboard import build_rental_dashboard

REPO = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "trailer-pw"
FORM = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture
async def trailer(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    await seed_rentals_from_config(pool, cfg)
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_rental_dashboard(
        cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector(), tool_handler_getter=lambda: None,
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/login", content=f"password={PASSWORD}", headers=FORM)
        yield client, pool, cfg, app


async def test_pages_require_a_session(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_rental_dashboard(cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard/trailer/inventory")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


async def test_test_page_renders_a_talk_button_for_the_trailer_ws(trailer):
    client, _pool, _cfg, _app = trailer
    body = (await client.get("/dashboard/trailer/test")).text
    assert "talk-btn" in body
    assert "/dashboard/trailer/testcall" in body


async def test_inventory_lists_trailer_types_and_rates(trailer):
    client, _pool, _cfg, _app = trailer
    body = (await client.get("/dashboard/trailer/inventory")).text
    assert "6x12 Utility" in body
    assert "$45.00" in body
    assert "7x14 Enclosed Cargo" in body


async def test_rentals_page_shows_a_booked_rental(trailer):
    client, pool, _cfg, _app = trailer
    unit = await pool.fetchval("SELECT id FROM trailer_units LIMIT 1")
    cust = await pool.fetchval(
        "INSERT INTO customers (name, phone) VALUES ('Ada Lovelace','+15550100') RETURNING id"
    )
    pickup = datetime.now(UTC).date() + timedelta(days=3)
    ret = pickup + timedelta(days=2)
    await pool.execute(
        "INSERT INTO rentals (trailer_unit_id, customer_id, pickup_date, return_date, rental_range,"
        " daily_rate, deposit, total_cost) VALUES ($1,$2,$3,$4,daterange($3,$4,'[]'),45,150,135)",
        unit, cust, pickup, ret,
    )
    body = (await client.get("/dashboard/trailer/rentals")).text
    assert "Ada Lovelace" in body
    assert "$135.00" in body
    assert "booked" in body


def test_testcall_requires_a_session(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    app = FastAPI()
    app.include_router(build_auth_router(cfg, PASSWORD))
    app.include_router(build_rental_dashboard(cfg, lambda: pool, PASSWORD, connect_gemini=FakeConnector()))
    client = TestClient(app)  # not logged in
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/dashboard/trailer/testcall") as ws:
        ws.receive_text()


async def test_rentals_page_renders_empty_state(trailer):
    client, pool, _cfg, _app = trailer
    await pool.execute("DELETE FROM rentals")
    body = (await client.get("/dashboard/trailer/rentals")).text
    assert "No rentals" in body
