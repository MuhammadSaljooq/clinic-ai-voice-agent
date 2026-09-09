"""RentalToolRouter against real Postgres."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from clinic_agent.agent.rental_tools import RentalToolRouter
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.db.rental_seed import seed_rentals_from_config
from clinic_agent.rental_config import load_rental_config

REPO = pathlib.Path(__file__).resolve().parents[1]
SECRET = "rtools-secret"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
PICKUP, RETURN = "2026-10-05", "2026-10-07"


class StubTelnyx:
    def __init__(self):
        self.transfers = []

    async def transfer(self, call_control_id, *, to):
        self.transfers.append((call_control_id, to))
        return {}


@pytest.fixture
async def router(pool):
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    await seed_rentals_from_config(pool, cfg)
    return RentalToolRouter(pool=pool, cfg=cfg, secret=SECRET, telnyx=StubTelnyx(),
                            clock=lambda: NOW), pool


def _ctx(caller="+15550123"):
    return ToolContext(caller_number=caller, call_control_id="ccid-1", state={})


async def test_find_returns_options_with_a_quote(router):
    route, _pool = router
    ctx = _ctx()
    res = await route("find_available_trailers",
                      {"trailer_type": "6x12 Utility", "pickup_date": PICKUP, "return_date": RETURN}, ctx)
    assert res["options"][0]["trailer"] == "6x12 Utility"
    assert res["options"][0]["days"] == 3
    assert res["options"][0]["total"] == "135.00"


async def test_book_by_option_writes_the_rental(router):
    route, pool = router
    ctx = _ctx(caller="")
    await route("find_available_trailers", {"trailer_type": "6x12 Utility", "pickup_date": PICKUP, "return_date": RETURN}, ctx)
    res = await route("book_rental",
                      {"option": 1, "first_name": "Ada", "last_name": "Lovelace",
                       "phone": "+15551230000", "email": "ada@example.com"}, ctx)
    assert res["booked"] is True
    row = await pool.fetchrow("SELECT c.name, c.email, r.total_cost FROM rentals r"
                              " JOIN customers c ON c.id=r.customer_id WHERE r.id=$1", res["rental_id"])
    assert row["name"] == "Ada Lovelace"
    assert row["email"] == "ada@example.com"


async def test_booking_an_unoffered_option_is_refused(router):
    route, _pool = router
    ctx = _ctx()
    await route("find_available_trailers", {"trailer_type": "6x12 Utility", "pickup_date": PICKUP, "return_date": RETURN}, ctx)
    res = await route("book_rental", {"option": 9, "first_name": "A", "last_name": "B", "phone": "+1"}, ctx)
    assert "booked" not in res
    assert "not one of the offered" in res["error"]


async def test_book_requires_first_last_and_phone(router):
    route, _pool = router
    ctx = _ctx(caller="")
    await route("find_available_trailers", {"trailer_type": "6x12 Utility", "pickup_date": PICKUP, "return_date": RETURN}, ctx)
    r1 = await route("book_rental", {"option": 1, "first_name": "Ada", "phone": "+1"}, ctx)
    assert "last" in r1["error"]
    r2 = await route("book_rental", {"option": 1, "first_name": "Ada", "last_name": "L"}, ctx)
    assert "phone" in r2["error"]


async def test_cancel_requires_prior_lookup(router):
    route, _pool = router
    ctx = _ctx(caller="")
    # book under a phone
    await route("find_available_trailers", {"trailer_type": "6x12 Utility", "pickup_date": PICKUP, "return_date": RETURN}, ctx)
    booked = await route("book_rental", {"option": 1, "first_name": "Ada", "last_name": "L", "phone": "+15550001"}, ctx)
    # a fresh call cannot cancel without looking it up first
    fresh = _ctx(caller="+15550001")
    blocked = await route("cancel_rental", {"rental_id": booked["rental_id"]}, fresh)
    assert "not been looked up" in blocked["error"]
    # after lookup, cancel works
    await route("lookup_rental", {}, fresh)
    ok = await route("cancel_rental", {"rental_id": booked["rental_id"]}, fresh)
    assert ok["cancelled"] is True


async def test_unknown_type_lists_available(router):
    route, _pool = router
    res = await route("find_available_trailers", {"trailer_type": "Spaceship", "pickup_date": PICKUP, "return_date": RETURN}, _ctx())
    assert "available_types" in res
    assert "6x12 Utility" in res["available_types"]


async def test_faq_and_transfer(router):
    route, _pool = router
    ans = await route("answer_faq", {"question": "do I need insurance?"}, _ctx())
    assert "insurance" in ans["answer"].lower()
    t = await route("transfer_to_human", {"reason": "billing"}, _ctx())
    assert t["transferring"] is True
