"""The tool router: the model's vocabulary against the real scheduler.

Runs on real Postgres and the shipped config.yaml. Two groups of tests here check
security properties rather than behaviour: that only an offered slot can be booked,
and that only an appointment looked up on *this* call can be changed.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from clinic_agent.agent.tools import ToolRouter
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.scheduling.booking import book as book_directly

REPO = pathlib.Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
SECRET = "router-test-secret"
CALLER = "+15550123"

MONDAY_4AM = datetime(2026, 9, 14, 4, 0, tzinfo=NY).astimezone(UTC)

FOLLOW_UP = "Follow-up"
PROCEDURE = "Procedure"  # Dr. Reyes only


class StubTelnyx:
    def __init__(self):
        self.transfers: list[tuple[str, str]] = []

    async def transfer(self, call_control_id, *, to):
        self.transfers.append((call_control_id, to))
        return {}


@pytest.fixture
async def router(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    telnyx = StubTelnyx()
    return (
        ToolRouter(
            pool=pool, cfg=cfg, secret=SECRET, telnyx=telnyx, clock=lambda: MONDAY_4AM
        ),
        telnyx,
        pool,
    )


def new_ctx(caller: str = CALLER) -> ToolContext:
    return ToolContext(caller_number=caller, call_control_id="ccid-1", state={})


# --- find_slots ---------------------------------------------------------------


async def test_find_slots_returns_numbered_options_and_keeps_tokens_private(router):
    route, _, _ = router
    ctx = new_ctx()

    result = await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)

    assert [o["option"] for o in result["options"]] == [1, 2, 3]
    assert result["options"][0]["when"] == "Monday the 14th at 9 am"
    assert result["options"][0]["provider"] == "Dr. Reyes"
    # The whole point of option numbers: no token reaches the model.
    assert "token" not in str(result)
    assert len(ctx.state["offers"]) == 3


async def test_find_slots_honours_a_part_of_day(router):
    route, _, _ = router
    result = await route(
        "find_slots", {"appointment_type": FOLLOW_UP, "part_of_day": "afternoon"}, new_ctx()
    )
    assert result["options"][0]["when"] == "Monday the 14th at 12 pm"


async def test_find_slots_honours_an_earliest_date(router):
    route, _, _ = router
    result = await route(
        "find_slots", {"appointment_type": FOLLOW_UP, "earliest_date": "2026-09-16"}, new_ctx()
    )
    assert "Wednesday" in result["options"][0]["when"]


async def test_find_slots_rejects_an_unknown_appointment_type_and_lists_the_real_ones(router):
    route, _, _ = router
    result = await route("find_slots", {"appointment_type": "Aromatherapy"}, new_ctx())
    assert "error" in result
    assert FOLLOW_UP in result["available_types"]


async def test_find_slots_rejects_an_unknown_provider(router):
    route, _, _ = router
    result = await route(
        "find_slots", {"appointment_type": FOLLOW_UP, "provider_name": "Dr. Nobody"}, new_ctx()
    )
    assert "error" in result
    assert "Dr. Reyes" in result["available_providers"]


async def test_find_slots_rejects_a_provider_who_does_not_offer_the_type(router):
    route, _, _ = router
    result = await route(
        "find_slots", {"appointment_type": PROCEDURE, "provider_name": "Dr. Osei"}, new_ctx()
    )
    assert "error" in result
    assert "recovery" in result


async def test_find_slots_rejects_a_malformed_date(router):
    route, _, _ = router
    result = await route(
        "find_slots", {"appointment_type": FOLLOW_UP, "earliest_date": "next tuesday"}, new_ctx()
    )
    assert "YYYY-MM-DD" in result["error"]


# --- booking ------------------------------------------------------------------


async def test_booking_an_offered_option_writes_the_appointment(router):
    route, _, pool = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)

    result = await route("book_appointment", {"option": 1, "patient_name": "Ada Lovelace"}, ctx)

    assert result["booked"] is True
    stored = await pool.fetchrow(
        "SELECT patient_id, starts_at FROM appointments WHERE id = $1", result["appointment_id"]
    )
    assert stored is not None
    phone = await pool.fetchval("SELECT phone FROM patients WHERE id = $1", stored["patient_id"])
    assert phone == CALLER, "should default to caller ID rather than asking"


async def test_booking_accepts_a_different_callback_number(router):
    route, _, pool = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)

    result = await route(
        "book_appointment",
        {"option": 1, "patient_name": "Ada", "callback_phone": "+15559999"},
        ctx,
    )
    stored = await pool.fetchval(
        "SELECT pt.phone FROM appointments a JOIN patients pt ON pt.id = a.patient_id"
        " WHERE a.id = $1",
        result["appointment_id"],
    )
    assert stored == "+15559999"


async def test_booking_an_option_that_was_never_offered_books_nothing(router):
    """The hallucination guarantee, at the phone line."""
    route, _, pool = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)

    result = await route("book_appointment", {"option": 99, "patient_name": "Ada"}, ctx)

    assert "error" in result
    assert await pool.fetchval("SELECT count(*) FROM appointments") == 0


async def test_booking_with_no_offers_at_all_is_refused(router):
    route, _, pool = router
    result = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, new_ctx())
    assert "error" in result
    assert await pool.fetchval("SELECT count(*) FROM appointments") == 0


async def test_booking_without_a_name_asks_for_one(router):
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route("book_appointment", {"option": 1}, ctx)
    assert "name" in result["error"]


async def test_booking_takes_first_last_phone_and_optional_email(router):
    route, _, pool = router
    ctx = new_ctx(caller="")  # no caller ID: the model must supply the phone
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route(
        "book_appointment",
        {"option": 1, "first_name": "Ada", "last_name": "Lovelace",
         "phone": "+15551230000", "email": "ada@example.com"},
        ctx,
    )
    assert result.get("booked") is True
    row = await pool.fetchrow(
        "SELECT name, phone, email FROM patients WHERE phone = $1", "+15551230000"
    )
    assert row["name"] == "Ada Lovelace"
    assert row["email"] == "ada@example.com"


async def test_booking_stores_the_reason_for_the_visit(router):
    route, _, pool = router
    ctx = new_ctx(caller="")
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route(
        "book_appointment",
        {"option": 1, "first_name": "Ada", "last_name": "Lovelace",
         "phone": "+15551230000", "reason": "persistent cough for a week"},
        ctx,
    )
    assert result.get("booked") is True
    stored = await pool.fetchval(
        "SELECT reason FROM appointments WHERE id = $1", result["appointment_id"]
    )
    assert stored == "persistent cough for a week"


async def test_booking_requires_both_first_and_last_name(router):
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route(
        "book_appointment", {"option": 1, "first_name": "Ada", "phone": "+15551230001"}, ctx
    )
    assert "booked" not in result
    assert "last" in result["error"]


async def test_booking_requires_a_phone_number(router):
    route, _, _ = router
    ctx = new_ctx(caller="")  # no caller ID and no phone given
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route(
        "book_appointment", {"option": 1, "first_name": "Ada", "last_name": "Lovelace"}, ctx
    )
    assert "booked" not in result
    assert "phone" in result["error"]


async def test_offers_are_consumed_so_one_call_cannot_book_the_same_slot_twice(router):
    route, _, pool = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    second = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    assert "error" in second
    assert await pool.fetchval("SELECT count(*) FROM appointments") == 1


async def test_a_slot_taken_mid_conversation_is_a_spoken_error_not_a_crash(router):
    """Someone else books the slot while the caller is deciding."""
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    stolen = ctx.state["offers"][1]

    await book_directly(
        route.pool,
        slot_token=stolen.token,
        secret=SECRET,
        patient_name="Someone Else",
        patient_phone="+15550777",
        now=MONDAY_4AM,
    )

    result = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    assert "taken" in result["error"]
    assert "find_slots" in result["recovery"]
    assert ctx.state["offers"] == {}, "stale offers must be cleared"


# --- lookup, reschedule, cancel ----------------------------------------------


async def test_lookup_finds_the_callers_appointment_from_caller_id_alone(router):
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    result = await route("lookup_appointment", {}, new_ctx())

    assert len(result["appointments"]) == 1
    assert result["appointments"][0]["provider"] == "Dr. Reyes"
    # Same wording as the offer path: "the 14th", not "the 14" (spoken as "the fourteen").
    assert result["appointments"][0]["when"] == "Monday the 14th at 9 am"


async def test_lookup_with_nothing_found_offers_a_way_forward(router):
    route, _, _ = router
    result = await route("lookup_appointment", {}, new_ctx(caller="+15550000"))
    assert result["appointments"] == []
    assert "recovery" in result


async def test_rescheduling_an_appointment_not_looked_up_is_refused(router):
    """Authorisation, not validation: a guessed id must not move someone else's slot."""
    route, _, pool = router
    owner = new_ctx(caller="+15550111")
    await route("find_slots", {"appointment_type": FOLLOW_UP}, owner)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Owner"}, owner)

    attacker = new_ctx(caller="+15559998")
    await route("find_slots", {"appointment_type": FOLLOW_UP}, attacker)
    result = await route(
        "reschedule_appointment",
        {"appointment_id": booked["appointment_id"], "option": 1},
        attacker,
    )

    assert "looked up" in result["error"]
    unchanged = await pool.fetchval(
        "SELECT starts_at FROM appointments WHERE id = $1", booked["appointment_id"]
    )
    assert unchanged is not None


async def test_cancelling_an_appointment_not_looked_up_is_refused(router):
    route, _, pool = router
    owner = new_ctx(caller="+15550111")
    await route("find_slots", {"appointment_type": FOLLOW_UP}, owner)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Owner"}, owner)

    attacker = new_ctx(caller="+15559998")
    result = await route(
        "cancel_appointment", {"appointment_id": booked["appointment_id"]}, attacker
    )

    assert "looked up" in result["error"]
    status = await pool.fetchval(
        "SELECT status FROM appointments WHERE id = $1", booked["appointment_id"]
    )
    assert str(status) == "booked", "a stranger must not be able to cancel this"


async def test_rescheduling_after_a_lookup_moves_the_appointment(router):
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    later = new_ctx()
    await route("lookup_appointment", {}, later)
    await route("find_slots", {"appointment_type": FOLLOW_UP, "part_of_day": "afternoon"}, later)
    result = await route(
        "reschedule_appointment",
        {"appointment_id": booked["appointment_id"], "option": 1},
        later,
    )

    assert result["rescheduled"] is True
    assert result["appointment_id"] == booked["appointment_id"], "identity must survive"
    assert "12 pm" in result["when"]


async def test_cancelling_after_a_lookup_frees_the_slot(router):
    route, _, pool = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    later = new_ctx()
    await route("lookup_appointment", {}, later)
    result = await route(
        "cancel_appointment", {"appointment_id": booked["appointment_id"]}, later
    )

    assert result["cancelled"] is True
    assert await pool.fetchval(
        "SELECT count(*) FROM appointments WHERE status = 'booked'"
    ) == 0


async def test_cancelling_twice_reports_cleanly(router):
    route, _, _ = router
    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)
    later = new_ctx()
    await route("lookup_appointment", {}, later)
    await route("cancel_appointment", {"appointment_id": booked["appointment_id"]}, later)

    again = await route("cancel_appointment", {"appointment_id": booked["appointment_id"]}, later)
    assert "already cancelled" in again["error"]


# --- FAQ and transfer ---------------------------------------------------------


async def test_faq_answers_a_known_question(router):
    route, _, _ = router
    result = await route("answer_faq", {"question": "where are you located"}, new_ctx())
    assert "Northside Avenue" in result["answer"]


async def test_faq_refuses_rather_than_guessing(router):
    route, _, _ = router
    result = await route("answer_faq", {"question": "my chest hurts, is that serious"}, new_ctx())
    assert "error" in result
    assert "not guess" in result["recovery"]


async def test_transfer_calls_telnyx_with_the_configured_number(router):
    route, telnyx, _ = router
    result = await route("transfer_to_human", {"reason": "clinical question"}, new_ctx())
    assert result["transferring"] is True
    assert telnyx.transfers == [("ccid-1", route.cfg.clinic.human_transfer_number)]


async def test_transfer_without_telephony_gives_the_caller_the_number(router):
    route, _, _ = router
    route.telnyx = None
    result = await route("transfer_to_human", {"reason": "x"}, new_ctx())
    assert route.cfg.clinic.human_transfer_number in result["recovery"]


async def test_an_unknown_tool_name_is_reported_not_raised(router):
    route, _, _ = router
    result = await route("order_a_pizza", {}, new_ctx())
    assert "unknown tool" in result["error"]


# --- full stack ---------------------------------------------------------------


async def test_a_scripted_call_books_a_real_appointment_end_to_end(router):
    """The proof that Plan 3 works: audio-layer tool calls reaching Postgres.

    Also proves per-call state survives between tool calls -- find_slots stores the
    offers and book_appointment reads them back, on the same ToolContext the bridge
    created when the call started.
    """
    from fakes import (
        FakeConnector,
        FakeGeminiSession,
        FakeTelnyxSocket,
        gemini_tool_call,
        telnyx_start,
        telnyx_stop,
    )

    from clinic_agent.bridge.call_session import CallSession

    route, _, pool = router

    gemini = FakeGeminiSession([
        gemini_tool_call("find_slots", {"appointment_type": FOLLOW_UP}, "fc-1"),
        gemini_tool_call(
            "book_appointment", {"option": 1, "patient_name": "Ada Lovelace"}, "fc-2"
        ),
    ])

    async def no_pacing(_seconds):
        return None

    session = CallSession(
        telnyx=FakeTelnyxSocket([telnyx_start(from_number=CALLER), telnyx_stop()]),
        connect_gemini=FakeConnector(gemini),
        tool_handler=route,
        max_reconnects=0,
        sleep=no_pacing,
    )
    outcome = await session.run()

    assert outcome.ended_reason == "telnyx_stop"
    assert len(gemini.tool_responses) == 2
    assert gemini.tool_responses[1].response["booked"] is True

    stored = await pool.fetchrow(
        "SELECT pt.name, pt.phone, a.starts_at FROM appointments a"
        " JOIN patients pt ON pt.id = a.patient_id WHERE a.status = 'booked'"
    )
    assert stored["name"] == "Ada Lovelace"
    assert stored["phone"] == CALLER, "caller ID was used without asking"


# --- calendar mirror wiring ----------------------------------------------------


class SpyMirror:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    async def on_booked(self, pool, appointment_id):
        self.calls.append(("booked", appointment_id))

    async def on_rescheduled(self, pool, appointment_id):
        self.calls.append(("rescheduled", appointment_id))

    async def on_cancelled(self, pool, appointment_id):
        self.calls.append(("cancelled", appointment_id))


class ExplodingMirror:
    async def on_booked(self, pool, appointment_id):
        raise RuntimeError("Google is down")


async def test_booking_rescheduling_and_cancelling_all_reach_the_calendar(router):
    route, _, _ = router
    spy = SpyMirror()
    route.mirror = spy

    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    booked = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    later = new_ctx()
    await route("lookup_appointment", {}, later)
    await route("find_slots", {"appointment_type": FOLLOW_UP, "part_of_day": "afternoon"}, later)
    await route("reschedule_appointment",
                {"appointment_id": booked["appointment_id"], "option": 1}, later)
    await route("cancel_appointment", {"appointment_id": booked["appointment_id"]}, later)

    assert spy.calls == [
        ("booked", booked["appointment_id"]),
        ("rescheduled", booked["appointment_id"]),
        ("cancelled", booked["appointment_id"]),
    ]


async def test_booking_still_succeeds_when_the_calendar_mirror_raises(router):
    """A real CalendarMirror swallows its own errors, but the router must not depend
    on that -- losing a booking because Google was down is unacceptable."""
    route, _, pool = router
    route.mirror = ExplodingMirror()

    ctx = new_ctx()
    await route("find_slots", {"appointment_type": FOLLOW_UP}, ctx)
    result = await route("book_appointment", {"option": 1, "patient_name": "Ada"}, ctx)

    assert result.get("booked") is True, f"booking was lost: {result}"
    assert await pool.fetchval("SELECT count(*) FROM appointments WHERE status='booked'") == 1
