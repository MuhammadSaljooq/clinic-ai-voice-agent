"""Booking against real Postgres.

The headline test is test_two_concurrent_bookings_of_one_slot_produce_one_winner.
Everything else is guardrails around it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from clinic_agent.scheduling.booking import (
    AppointmentNotFound,
    Booked,
    ProviderCannotPerformType,
    SlotTaken,
    book,
    cancel,
    find_upcoming_appointments,
    load_busy,
    reschedule,
)
from clinic_agent.scheduling.models import Slot
from clinic_agent.scheduling.tokens import InvalidSlotToken, issue_slot_token

TEST_SECRET = "test-secret-not-for-production"

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
AT_3PM = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)

FOLLOW_UP = 1  # 30 min, 15 min after-buffer, both providers
NEW_PATIENT = 2  # 60 min, 10 min after-buffer, provider 1 only


def token(start, *, provider=1, type_id=FOLLOW_UP, minutes=30):
    slot = Slot(
        provider_id=provider,
        appointment_type_id=type_id,
        start=start,
        end=start + timedelta(minutes=minutes),
    )
    return issue_slot_token(slot, TEST_SECRET, now=NOW)


async def book_at(pool, start, **kw):
    # Pop the patient args *before* forwarding the rest to token().
    patient_name = kw.pop("patient_name", "Ada Lovelace")
    patient_phone = kw.pop("patient_phone", "+15550100")
    return await book(
        pool,
        slot_token=token(start, **kw),
        secret=TEST_SECRET,
        patient_name=patient_name,
        patient_phone=patient_phone,
        now=NOW,
    )


async def test_booking_writes_an_appointment(clinic):
    result = await book_at(clinic, AT_3PM)
    assert isinstance(result, Booked)
    row = await clinic.fetchrow(
        "SELECT provider_id, starts_at, ends_at, status FROM appointments WHERE id = $1",
        result.appointment_id,
    )
    assert row["provider_id"] == 1
    assert row["starts_at"] == AT_3PM
    assert row["ends_at"] == AT_3PM + timedelta(minutes=30)
    assert row["status"] == "booked"


async def test_booking_the_same_slot_twice_raises_slot_taken(clinic):
    await book_at(clinic, AT_3PM)
    with pytest.raises(SlotTaken):
        await book_at(clinic, AT_3PM, patient_phone="+15550199")


async def test_two_concurrent_bookings_of_one_slot_produce_one_winner(clinic):
    """The property everything else depends on.

    Two callers race for the last slot on separate connections. Postgres must let
    exactly one through -- not zero, not two.
    """
    results = await asyncio.gather(
        book(
            clinic,
            slot_token=token(AT_3PM),
            secret=TEST_SECRET,
            patient_name="Caller One",
            patient_phone="+15550001",
            now=NOW,
        ),
        book(
            clinic,
            slot_token=token(AT_3PM),
            secret=TEST_SECRET,
            patient_name="Caller Two",
            patient_phone="+15550002",
            now=NOW,
        ),
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, Booked)]
    losers = [r for r in results if isinstance(r, SlotTaken)]
    unexpected = [r for r in results if not isinstance(r, Booked | SlotTaken)]

    assert not unexpected, f"unexpected failures: {unexpected!r}"
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == 1

    stored = await clinic.fetchval(
        "SELECT count(*) FROM appointments WHERE status = 'booked' AND starts_at = $1", AT_3PM
    )
    assert stored == 1


async def test_booking_inside_a_buffer_is_rejected(clinic):
    """15:00-15:30 has a 15-min after-buffer, so 15:30 must not be bookable."""
    await book_at(clinic, AT_3PM)
    with pytest.raises(SlotTaken):
        await book_at(clinic, AT_3PM + timedelta(minutes=30), patient_phone="+15550199")


async def test_booking_clear_of_the_buffer_succeeds(clinic):
    await book_at(clinic, AT_3PM)
    result = await book_at(clinic, AT_3PM + timedelta(minutes=45), patient_phone="+15550199")
    assert result.starts_at == AT_3PM + timedelta(minutes=45)


async def test_the_same_time_is_bookable_for_a_different_provider(clinic):
    await book_at(clinic, AT_3PM, provider=1)
    result = await book_at(clinic, AT_3PM, provider=2, patient_phone="+15550199")
    assert result.provider_id == 2


async def test_cancelling_frees_the_slot(clinic):
    first = await book_at(clinic, AT_3PM)
    await cancel(clinic, first.appointment_id)
    again = await book_at(clinic, AT_3PM, patient_phone="+15550199")
    assert again.appointment_id != first.appointment_id


async def test_cancelling_twice_raises_not_found(clinic):
    first = await book_at(clinic, AT_3PM)
    await cancel(clinic, first.appointment_id)
    with pytest.raises(AppointmentNotFound):
        await cancel(clinic, first.appointment_id)


async def test_reschedule_moves_the_appointment_keeping_its_identity(clinic):
    """Identity must survive, so the calendar mirror and any reminder still match."""
    original = await book_at(clinic, AT_3PM)
    later = AT_3PM + timedelta(hours=2)

    moved = await reschedule(
        clinic,
        appointment_id=original.appointment_id,
        slot_token=token(later),
        secret=TEST_SECRET,
        now=NOW,
    )

    assert moved.appointment_id == original.appointment_id
    assert moved.patient_id == original.patient_id
    assert moved.starts_at == later
    assert await clinic.fetchval("SELECT count(*) FROM appointments") == 1


async def test_reschedule_onto_a_taken_slot_leaves_the_original_intact(clinic):
    mine = await book_at(clinic, AT_3PM)
    theirs_at = AT_3PM + timedelta(hours=2)
    await book_at(clinic, theirs_at, patient_phone="+15550199")

    with pytest.raises(SlotTaken):
        await reschedule(
            clinic,
            appointment_id=mine.appointment_id,
            slot_token=token(theirs_at),
            secret=TEST_SECRET,
            now=NOW,
        )

    assert (
        await clinic.fetchval("SELECT starts_at FROM appointments WHERE id = $1", mine.appointment_id)
        == AT_3PM
    ), "a failed reschedule must not move or lose the original"


async def test_a_forged_token_cannot_book(clinic):
    with pytest.raises(InvalidSlotToken):
        await book(
            clinic,
            slot_token=token(AT_3PM).replace(".", ".X", 1),
            secret=TEST_SECRET,
            patient_name="Attacker",
            patient_phone="+15550666",
            now=NOW,
        )
    assert await clinic.fetchval("SELECT count(*) FROM appointments") == 0


async def test_provider_who_does_not_offer_the_type_is_rejected(clinic):
    """Provider 2 does not do New patient appointments."""
    with pytest.raises(ProviderCannotPerformType):
        await book_at(clinic, AT_3PM, provider=2, type_id=NEW_PATIENT, minutes=60)


async def test_token_duration_disagreeing_with_the_type_is_rejected(clinic):
    """A 90-min token for a 30-min type means config and token drifted apart."""
    with pytest.raises(ValueError, match="token duration"):
        await book_at(clinic, AT_3PM, minutes=90)


async def test_load_busy_returns_appointments_with_their_buffers(clinic):
    await book_at(clinic, AT_3PM)
    busy = await load_busy(
        clinic,
        provider_ids=[1, 2],
        window_start=AT_3PM - timedelta(hours=1),
        window_end=AT_3PM + timedelta(hours=1),
    )
    assert len(busy) == 1
    assert busy[0].start == AT_3PM
    assert busy[0].buffer_after_min == 15, "find_slots needs the buffer to block 15:30"


async def test_load_busy_ignores_cancelled_appointments(clinic):
    first = await book_at(clinic, AT_3PM)
    await cancel(clinic, first.appointment_id)
    busy = await load_busy(
        clinic,
        provider_ids=[1],
        window_start=AT_3PM - timedelta(hours=1),
        window_end=AT_3PM + timedelta(hours=1),
    )
    assert busy == []


async def test_many_concurrent_bookings_never_produce_unexpected_errors(clinic):
    """Regression test for deadlocks on the exclusion-constrained index.

    Concurrent INSERTs of overlapping ranges each write a speculative index entry
    and then scan for conflicts. If two transactions interleave so that each scans
    after the other has written, they wait on each other's transaction id and
    Postgres kills one with DeadlockDetectedError after deadlock_timeout (1s)
    instead of a clean ExclusionViolationError.

    Measured before the fix: 188 deadlocks across 550 races. A caller would hear a
    crash instead of "that slot just went". Five callers per round makes the window
    easy to hit.
    """
    rounds, callers = 12, 5
    for round_index in range(rounds):
        start = AT_3PM + timedelta(hours=round_index)  # spaced so rounds cannot interfere
        results = await asyncio.gather(
            *[
                book(
                    clinic,
                    slot_token=token(start),
                    secret=TEST_SECRET,
                    patient_name=f"Caller {k}",
                    patient_phone=f"+1555{k:07d}",
                    now=NOW,
                )
                for k in range(callers)
            ],
            return_exceptions=True,
        )
        unexpected = [r for r in results if not isinstance(r, Booked | SlotTaken)]
        winners = [r for r in results if isinstance(r, Booked)]
        assert not unexpected, f"round {round_index}: {unexpected!r}"
        assert len(winners) == 1, f"round {round_index}: {len(winners)} winners"


# --- looking up a caller's existing appointments -------------------------------


async def test_lookup_finds_a_booked_appointment_by_phone(clinic):
    booked = await book_at(clinic, AT_3PM, patient_phone="+15550123")
    found = await find_upcoming_appointments(clinic, phone="+15550123", now=NOW)

    assert len(found) == 1
    assert found[0].appointment_id == booked.appointment_id
    assert found[0].provider_name == "Dr. Reyes"
    assert found[0].appointment_type_name == "Follow-up"


async def test_lookup_ignores_cancelled_appointments(clinic):
    booked = await book_at(clinic, AT_3PM, patient_phone="+15550123")
    await cancel(clinic, booked.appointment_id)
    assert await find_upcoming_appointments(clinic, phone="+15550123", now=NOW) == []


async def test_lookup_ignores_appointments_already_in_the_past(clinic):
    """Offering to reschedule yesterday's appointment would be nonsense."""
    await book_at(clinic, AT_3PM, patient_phone="+15550123")
    later = AT_3PM + timedelta(days=1)
    assert await find_upcoming_appointments(clinic, phone="+15550123", now=later) == []


async def test_lookup_matches_a_name_case_insensitively(clinic):
    await book_at(clinic, AT_3PM, patient_name="Ada Lovelace", patient_phone="+15550123")
    found = await find_upcoming_appointments(clinic, name="ada lovelace", now=NOW)
    assert len(found) == 1


async def test_lookup_returns_the_soonest_appointment_first(clinic):
    later = await book_at(clinic, AT_3PM + timedelta(hours=3), patient_phone="+15550123")
    sooner = await book_at(clinic, AT_3PM, patient_phone="+15550123")

    found = await find_upcoming_appointments(clinic, phone="+15550123", now=NOW)
    assert [a.appointment_id for a in found] == [sooner.appointment_id, later.appointment_id]


async def test_lookup_does_not_return_other_patients_appointments(clinic):
    await book_at(clinic, AT_3PM, patient_phone="+15550111")
    assert await find_upcoming_appointments(clinic, phone="+15550999", now=NOW) == []


async def test_lookup_without_phone_or_name_is_refused(clinic):
    """Returning every patient's appointments would be a privacy failure."""
    with pytest.raises(ValueError, match="requires phone or name"):
        await find_upcoming_appointments(clinic, now=NOW)


async def test_rescheduling_clears_a_stale_reminder_for_the_old_time(clinic):
    """A reminder already sent for the old time would otherwise block the worker from
    ever reminding the patient about the new time."""
    booked = await book_at(clinic, AT_3PM)
    await clinic.execute(
        "INSERT INTO reminders (appointment_id, scheduled_for, status, body, attempts)"
        " VALUES ($1, now(), 'sent', 'reminder for the old time', 1)",
        booked.appointment_id,
    )

    await reschedule(
        clinic,
        appointment_id=booked.appointment_id,
        slot_token=token(AT_3PM + timedelta(days=1)),
        secret=TEST_SECRET,
        now=NOW,
    )

    remaining = await clinic.fetchval(
        "SELECT count(*) FROM reminders WHERE appointment_id = $1", booked.appointment_id
    )
    assert remaining == 0, "the stale reminder must be cleared so a fresh one can go out"
