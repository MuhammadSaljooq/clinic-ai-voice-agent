"""The handyman tool router against real Postgres and the shipped config.

Checks each intake tool writes the right row, that a phone is required where it should be,
and that the caller ID is used as a fallback.
"""

from __future__ import annotations

import pathlib

import pytest

from clinic_agent.agent.handyman_tools import HandymanToolRouter
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.handyman_config import load_handyman_config

REPO = pathlib.Path(__file__).resolve().parents[1]
CALLER = "+18655550123"


class StubTelnyx:
    def __init__(self):
        self.transfers: list[tuple[str, str]] = []

    async def transfer(self, call_control_id, *, to):
        self.transfers.append((call_control_id, to))
        return {}


@pytest.fixture
async def router(pool):
    cfg = load_handyman_config(REPO / "handyman_config.yaml")
    telnyx = StubTelnyx()
    return HandymanToolRouter(pool=pool, cfg=cfg, telnyx=telnyx), telnyx, pool


def new_ctx(caller: str = CALLER) -> ToolContext:
    return ToolContext(caller_number=caller, call_control_id="ccid-1", state={})


async def test_answer_question_uses_the_configured_faq(router):
    route, _, _ = router
    res = await route("answer_question", {"question": "do you do free estimates?"}, new_ctx())
    assert "answer" in res
    assert "estimate" in res["answer"].lower()


async def test_answer_question_refuses_to_guess_a_price(router):
    route, _, _ = router
    res = await route("answer_question", {"question": "what's the meaning of life"}, new_ctx())
    assert "error" in res
    assert "price" in res["recovery"].lower()


async def test_request_appointment_writes_a_full_request(router):
    route, _, pool = router
    res = await route(
        "request_appointment",
        {"name": "Dana Scully", "phone": "+18655551111", "email": "dana@example.com",
         "job_type": "drywall repair", "description": "hole in the hallway wall",
         "address": "West Knoxville", "preferred_time": "Thursday morning"},
        new_ctx(caller=""),
    )
    assert res.get("requested") is True
    row = await pool.fetchrow(
        "SELECT name, phone, email, job_type, address, preferred_time, status::text AS status"
        " FROM handyman_appointment_requests WHERE phone=$1", "+18655551111"
    )
    assert row["name"] == "Dana Scully"
    assert row["email"] == "dana@example.com"
    assert row["job_type"] == "drywall repair"
    assert row["status"] == "requested"


async def test_request_appointment_falls_back_to_caller_id_for_the_phone(router):
    route, _, pool = router
    res = await route(
        "request_appointment",
        {"name": "Dana Scully", "email": "dana@example.com", "description": "fix a fence"},
        new_ctx(),  # phone comes from caller ID
    )
    assert res.get("requested") is True
    count = await pool.fetchval(
        "SELECT count(*) FROM handyman_appointment_requests WHERE phone=$1", CALLER
    )
    assert count == 1


async def test_request_appointment_requires_a_name(router):
    route, _, _ = router
    res = await route(
        "request_appointment", {"phone": "+18655551111", "email": "x@y.com"}, new_ctx(caller="")
    )
    assert "requested" not in res
    assert "name" in res["error"]


async def test_request_appointment_needs_a_phone_when_there_is_no_caller_id(router):
    route, _, _ = router
    res = await route(
        "request_appointment", {"name": "Dana", "email": "x@y.com"}, new_ctx(caller="")
    )
    assert "requested" not in res
    assert "phone" in res["error"]


async def test_request_appointment_requires_an_email(router):
    route, _, _ = router
    res = await route(
        "request_appointment", {"name": "Dana", "phone": "+18655551111"}, new_ctx(caller="")
    )
    assert "requested" not in res
    assert "email" in res["error"]
    assert "capture_lead" in res["recovery"]  # offer a callback lead if they have no email


async def test_request_appointment_rejects_a_malformed_email(router):
    route, _, _ = router
    res = await route(
        "request_appointment",
        {"name": "Dana", "phone": "+18655551111", "email": "dana-at-example"},
        new_ctx(caller=""),
    )
    assert "requested" not in res
    assert "valid" in res["error"]


async def test_capture_lead_writes_a_lead(router):
    route, _, pool = router
    res = await route(
        "capture_lead",
        {"name": "Fox Mulder", "phone": "+18655552222", "reason": "wants a callback about a deck"},
        new_ctx(caller=""),
    )
    assert res.get("queued") is True
    row = await pool.fetchrow(
        "SELECT name, reason, status::text AS status FROM handyman_leads WHERE phone=$1",
        "+18655552222",
    )
    assert row["name"] == "Fox Mulder"
    assert row["status"] == "new"


async def test_capture_lead_needs_a_phone(router):
    route, _, _ = router
    res = await route("capture_lead", {"name": "No Number"}, new_ctx(caller=""))
    assert "queued" not in res
    assert "phone" in res["error"]


async def test_take_message_writes_a_personal_message(router):
    route, _, pool = router
    res = await route(
        "take_message",
        {"caller_name": "Mom", "phone": "+18655553333", "message": "call me about Sunday dinner"},
        new_ctx(caller=""),
    )
    assert res.get("taken") is True
    row = await pool.fetchrow(
        "SELECT caller_name, message, status::text AS status FROM handyman_messages WHERE phone=$1",
        "+18655553333",
    )
    assert row["caller_name"] == "Mom"
    assert "Sunday dinner" in row["message"]
    assert row["status"] == "new"


async def test_take_message_needs_an_actual_message(router):
    route, _, _ = router
    res = await route("take_message", {"caller_name": "Someone"}, new_ctx())
    assert "taken" not in res
    assert "message" in res["error"]


async def test_transfer_hands_off_to_the_owners_number(router):
    route, telnyx, _ = router
    res = await route("transfer_to_human", {"reason": "wants to talk to Michael"}, new_ctx())
    assert res.get("transferring") is True
    assert telnyx.transfers and telnyx.transfers[0][0] == "ccid-1"


async def test_an_unknown_tool_is_handled_gracefully(router):
    route, _, _ = router
    res = await route("do_something_weird", {}, new_ctx())
    assert "error" in res
