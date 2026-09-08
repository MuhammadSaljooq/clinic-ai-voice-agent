"""Calendar mirror. The property that matters most: it never breaks a booking."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clinic_agent.calendar_mirror.events import APPOINTMENT_ID_KEY, event_body
from clinic_agent.calendar_mirror.sync import CalendarMirror
from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config

REPO = pathlib.Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
CALENDAR = "clinic@example.test"


class FakeCalendar:
    def __init__(self, *, fail_on: set[str] | None = None):
        self.events: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_on = fail_on or set()
        self._next = 0

    def _maybe_fail(self, op):
        self.calls.append(op)
        if op in self.fail_on:
            raise RuntimeError(f"Google said no to {op}")

    def insert(self, calendar_id, body):
        self._maybe_fail("insert")
        self._next += 1
        event_id = f"evt-{self._next}"
        self.events[event_id] = body
        return {"id": event_id, **body}

    def patch(self, calendar_id, event_id, body):
        self._maybe_fail("patch")
        self.events[event_id] = body
        return {"id": event_id, **body}

    def delete(self, calendar_id, event_id):
        self._maybe_fail("delete")
        self.events.pop(event_id, None)


# --- the event body (pure) -----------------------------------------------------


def test_event_body_names_the_patient_and_the_appointment_type():
    body = event_body(
        appointment_id=7, patient_name="Ada Lovelace", patient_phone="+15550100",
        provider_name="Dr. Reyes", appointment_type_name="Follow-up",
        starts_at=NOW, ends_at=NOW + timedelta(minutes=15), tz=NY,
    )
    assert body["summary"] == "Ada Lovelace - Follow-up"
    assert "Dr. Reyes" in body["description"]
    assert "#7" in body["description"]


def test_event_times_carry_an_offset_and_an_explicit_timezone():
    body = event_body(
        appointment_id=1, patient_name="A", patient_phone="+1", provider_name="P",
        appointment_type_name="T", starts_at=NOW, ends_at=NOW + timedelta(minutes=30), tz=NY,
    )
    assert body["start"]["timeZone"] == "America/New_York"
    assert body["start"]["dateTime"].startswith("2026-09-14T08:00")  # 12:00 UTC is 08:00 EDT
    assert "-04:00" in body["start"]["dateTime"]


def test_event_carries_the_appointment_id_for_reconciliation():
    """Queryable, rather than requiring the description to be parsed."""
    body = event_body(
        appointment_id=42, patient_name="A", patient_phone="+1", provider_name="P",
        appointment_type_name="T", starts_at=NOW, ends_at=NOW, tz=NY,
    )
    assert body["extendedProperties"]["private"][APPOINTMENT_ID_KEY] == "42"


# --- syncing -------------------------------------------------------------------


@pytest.fixture
async def mirrored(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    calendar = FakeCalendar()
    return pool, cfg, calendar, CalendarMirror(calendar, CALENDAR, cfg)


async def make_appointment(pool, *, hours=24):
    patient_id = await pool.fetchval(
        "INSERT INTO patients (name, phone) VALUES ('Ada Lovelace', '+15550100') RETURNING id"
    )
    starts = NOW + timedelta(hours=hours)
    return await pool.fetchval(
        "INSERT INTO appointments (provider_id, appointment_type_id, patient_id,"
        " starts_at, ends_at, blocked_range) VALUES (1,1,$1,$2,$3,tstzrange($2,$3))"
        " RETURNING id",
        patient_id, starts, starts + timedelta(minutes=15),
    )


async def test_booking_creates_an_event_and_records_its_id(mirrored):
    pool, _cfg, calendar, mirror = mirrored
    appointment = await make_appointment(pool)

    event_id = await mirror.on_booked(pool, appointment)

    assert event_id == "evt-1"
    assert calendar.events["evt-1"]["summary"] == "Ada Lovelace - Follow-up"
    stored = await pool.fetchval("SELECT gcal_event_id FROM appointments WHERE id=$1", appointment)
    assert stored == "evt-1"


async def test_rescheduling_patches_the_existing_event_rather_than_duplicating_it(mirrored):
    pool, _cfg, calendar, mirror = mirrored
    appointment = await make_appointment(pool)
    await mirror.on_booked(pool, appointment)

    new_start = NOW + timedelta(hours=48)
    await pool.execute(
        "UPDATE appointments SET starts_at=$2, ends_at=$3,"
        " blocked_range=tstzrange($2,$3) WHERE id=$1",
        appointment, new_start, new_start + timedelta(minutes=15),
    )
    await mirror.on_rescheduled(pool, appointment)

    assert len(calendar.events) == 1, "a second event would double-book the provider's diary"
    assert calendar.calls == ["insert", "patch"]


async def test_rescheduling_repairs_a_booking_whose_mirror_failed(mirrored):
    """A gap should heal, not persist forever."""
    pool, cfg, _calendar, _mirror = mirrored
    failing = FakeCalendar(fail_on={"insert"})
    mirror = CalendarMirror(failing, CALENDAR, cfg)
    appointment = await make_appointment(pool)

    assert await mirror.on_booked(pool, appointment) is None
    failing.fail_on.clear()
    event_id = await mirror.on_rescheduled(pool, appointment)

    assert event_id is not None
    assert await pool.fetchval(
        "SELECT gcal_event_id FROM appointments WHERE id=$1", appointment
    ) == event_id


async def test_cancelling_deletes_the_event(mirrored):
    """Marking it would leave a cancelled appointment on a provider's phone."""
    pool, _cfg, calendar, mirror = mirrored
    appointment = await make_appointment(pool)
    await mirror.on_booked(pool, appointment)

    await mirror.on_cancelled(pool, appointment)

    assert calendar.events == {}
    assert await pool.fetchval(
        "SELECT gcal_event_id FROM appointments WHERE id=$1", appointment
    ) is None


async def test_cancelling_something_never_mirrored_is_not_an_error(mirrored):
    pool, _cfg, calendar, mirror = mirrored
    appointment = await make_appointment(pool)
    await mirror.on_cancelled(pool, appointment)
    assert calendar.calls == []


# --- the mirror must never break a booking -------------------------------------


async def test_a_google_failure_on_create_leaves_the_appointment_intact(mirrored):
    """Losing a calendar entry is an annoyance. Losing a booking is not acceptable."""
    pool, cfg, _calendar, _mirror = mirrored
    mirror = CalendarMirror(FakeCalendar(fail_on={"insert"}), CALENDAR, cfg)
    appointment = await make_appointment(pool)

    assert await mirror.on_booked(pool, appointment) is None  # no exception

    row = await pool.fetchrow(
        "SELECT status::text AS status, gcal_event_id FROM appointments WHERE id=$1", appointment
    )
    assert row["status"] == "booked"
    assert row["gcal_event_id"] is None, "left unset so reconciliation can find it"


async def test_a_google_failure_on_delete_does_not_raise(mirrored):
    pool, cfg, _calendar, _mirror = mirrored
    calendar = FakeCalendar()
    mirror = CalendarMirror(calendar, CALENDAR, cfg)
    appointment = await make_appointment(pool)
    await mirror.on_booked(pool, appointment)

    calendar.fail_on.add("delete")
    await mirror.on_cancelled(pool, appointment)  # must not raise

    assert await pool.fetchval(
        "SELECT gcal_event_id FROM appointments WHERE id=$1", appointment
    ) == "evt-1", "id kept so the stale event can be cleaned up later"


async def test_mirroring_an_appointment_that_does_not_exist_is_safe(mirrored):
    pool, _cfg, calendar, mirror = mirrored
    assert await mirror.on_booked(pool, 999999) is None
    assert calendar.calls == []
