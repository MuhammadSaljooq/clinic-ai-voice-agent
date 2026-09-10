"""Transactional booking, rescheduling and cancellation.

The database is the authority. `find_slots` decides what *should* be bookable, but
the exclusion constraint decides what actually gets written -- so two callers racing
for the last slot cannot both win, no matter how the application behaves.

Failures come back as typed results, not raw driver errors, because the voice agent
needs to say something useful ("that one just went, how about...") rather than crash
mid-sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import asyncpg

from clinic_agent.scheduling.models import AvailabilityException, Busy
from clinic_agent.scheduling.tokens import verify_slot_token

# Namespace for pg_advisory_xact_lock so our keys cannot collide with any other
# advisory-lock user in the same database. Arbitrary but must stay stable.
APPOINTMENT_LOCK_CLASS = 19731


async def _serialize_provider_writes(conn: asyncpg.Connection, provider_id: int) -> None:
    """Serialize appointment writes for one provider within this transaction.

    Without this, concurrent INSERTs of overlapping ranges each write a speculative
    entry into the exclusion-constrained index and then scan for conflicts. Interleave
    them and each ends up waiting on the other's transaction id, so Postgres kills one
    with DeadlockDetectedError after deadlock_timeout rather than raising a clean
    ExclusionViolationError -- the caller hears a crash instead of "that slot just went".

    Measured before this lock: 188 deadlocks across 550 races.

    Taking it *first*, before the patient row lock and the index insert, also gives every
    write transaction one consistent lock order. The lock is released automatically when
    the transaction ends, and only contends between writes for the *same* provider, which
    for a clinic is a handful of events per minute.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1, $2)", APPOINTMENT_LOCK_CLASS, provider_id
    )


class SlotTaken(Exception):
    """Someone else booked this slot first, or it collides with a buffer."""


class AppointmentNotFound(Exception):
    """No active appointment with that id."""


class ProviderCannotPerformType(Exception):
    """The provider is not configured to perform this appointment type."""


class UnknownAppointmentType(Exception):
    """No active appointment type with that id."""


@dataclass(frozen=True, slots=True)
class Booked:
    appointment_id: int
    provider_id: int
    appointment_type_id: int
    patient_id: int
    starts_at: datetime
    ends_at: datetime


async def _upsert_patient(
    conn: asyncpg.Connection, name: str, phone: str, email: str | None = None
) -> int:
    # COALESCE on email so a later booking without an email does not wipe one already
    # on file for this number.
    return await conn.fetchval(
        """
        INSERT INTO patients (name, phone, email) VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE
        SET name = EXCLUDED.name,
            email = COALESCE(EXCLUDED.email, patients.email)
        RETURNING id
        """,
        name,
        phone,
        email,
    )


async def _type_row(conn: asyncpg.Connection, appointment_type_id: int) -> asyncpg.Record:
    row = await conn.fetchrow(
        """
        SELECT duration_min, buffer_before_min, buffer_after_min
        FROM appointment_types WHERE id = $1 AND active
        """,
        appointment_type_id,
    )
    if row is None:
        raise UnknownAppointmentType(f"appointment type {appointment_type_id} is unknown or inactive")
    return row


async def _assert_provider_eligible(
    conn: asyncpg.Connection, provider_id: int, appointment_type_id: int
) -> None:
    eligible = await conn.fetchval(
        """
        SELECT 1 FROM appointment_type_providers
        WHERE provider_id = $1 AND appointment_type_id = $2
        """,
        provider_id,
        appointment_type_id,
    )
    if not eligible:
        raise ProviderCannotPerformType(
            f"provider {provider_id} does not perform appointment type {appointment_type_id}"
        )


async def book(
    pool: asyncpg.Pool,
    *,
    slot_token: str,
    secret: str,
    patient_name: str,
    patient_phone: str,
    patient_email: str | None = None,
    reason: str | None = None,
    source: str = "voice_agent",
    now: datetime | None = None,
) -> Booked:
    """Book a slot previously issued by find_slots.

    Only a valid, unexpired token is accepted, so a time the model invented cannot
    be booked. Raises SlotTaken if the slot is gone by the time we commit.
    """
    slot = verify_slot_token(slot_token, secret, now=now)

    async with pool.acquire() as conn, conn.transaction():
        await _serialize_provider_writes(conn, slot.provider_id)

        type_row = await _type_row(conn, slot.appointment_type_id)
        await _assert_provider_eligible(conn, slot.provider_id, slot.appointment_type_id)

        # Defence in depth: a token whose duration disagrees with the type's
        # configured duration means the two drifted apart. Refuse rather than book
        # something nobody intended.
        expected = timedelta(minutes=type_row["duration_min"])
        if slot.end - slot.start != expected:
            raise ValueError(
                f"token duration {slot.end - slot.start} != type duration {expected}"
            )

        patient_id = await _upsert_patient(conn, patient_name, patient_phone, patient_email)

        blocked_start = slot.start - timedelta(minutes=type_row["buffer_before_min"])
        blocked_end = slot.end + timedelta(minutes=type_row["buffer_after_min"])

        try:
            appointment_id = await conn.fetchval(
                """
                INSERT INTO appointments (
                    provider_id, appointment_type_id, patient_id,
                    starts_at, ends_at, buffer_before_min, buffer_after_min,
                    blocked_range, source, reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, tstzrange($8, $9), $10, $11)
                RETURNING id
                """,
                slot.provider_id,
                slot.appointment_type_id,
                patient_id,
                slot.start,
                slot.end,
                type_row["buffer_before_min"],
                type_row["buffer_after_min"],
                blocked_start,
                blocked_end,
                source,
                reason,
            )
        except (
            asyncpg.exceptions.ExclusionViolationError,
            # Should be unreachable now that writes are serialized per provider, but
            # kept so a future write path that forgets the lock degrades into a clean
            # "slot taken" instead of surfacing a raw driver error to a caller.
            asyncpg.exceptions.DeadlockDetectedError,
        ) as exc:
            raise SlotTaken(
                f"{slot.start.isoformat()} is no longer available for provider {slot.provider_id}"
            ) from exc

    return Booked(
        appointment_id=appointment_id,
        provider_id=slot.provider_id,
        appointment_type_id=slot.appointment_type_id,
        patient_id=patient_id,
        starts_at=slot.start,
        ends_at=slot.end,
    )


async def _reset_reminder(conn: asyncpg.Connection, appointment_id: int) -> None:
    """Drop the reminder bookkeeping for an appointment whose time is changing.

    `reminders.appointment_id` is UNIQUE, and the worker only re-selects rows that are
    absent or 'failed' -- so a reminder already 'sent'/'dry_run'/'skipped' for the OLD
    time would otherwise permanently suppress a reminder for the NEW time, and a patient
    who moved their appointment would silently never be reminded. Deleting the row lets
    the worker schedule a fresh reminder; the text actually sent for the old time is
    preserved in the message log, so nothing auditable is lost.
    """
    await conn.execute("DELETE FROM reminders WHERE appointment_id = $1", appointment_id)


async def cancel(pool: asyncpg.Pool, appointment_id: int) -> None:
    """Cancel a booked appointment. Idempotent only in the sense that
    cancelling twice raises AppointmentNotFound the second time."""
    async with pool.acquire() as conn:
        cancelled = await conn.fetchval(
            """
            UPDATE appointments SET status = 'cancelled'
            WHERE id = $1 AND status = 'booked'
            RETURNING id
            """,
            appointment_id,
        )
    if cancelled is None:
        raise AppointmentNotFound(f"no booked appointment with id {appointment_id}")


async def reschedule(
    pool: asyncpg.Pool,
    *,
    appointment_id: int,
    slot_token: str,
    secret: str,
    now: datetime | None = None,
) -> Booked:
    """Move an existing appointment to a new slot, atomically.

    The row keeps its identity, so the Google Calendar mirror and any reminder
    already scheduled continue to point at the same appointment.
    """
    slot = verify_slot_token(slot_token, secret, now=now)

    async with pool.acquire() as conn, conn.transaction():
        # The destination provider's index is where a conflict can occur; moving a row
        # away from a provider only deletes an index entry, which never conflicts.
        await _serialize_provider_writes(conn, slot.provider_id)

        existing = await conn.fetchrow(
            "SELECT patient_id, status FROM appointments WHERE id = $1 FOR UPDATE",
            appointment_id,
        )
        if existing is None or existing["status"] != "booked":
            raise AppointmentNotFound(f"no booked appointment with id {appointment_id}")

        type_row = await _type_row(conn, slot.appointment_type_id)
        await _assert_provider_eligible(conn, slot.provider_id, slot.appointment_type_id)

        blocked_start = slot.start - timedelta(minutes=type_row["buffer_before_min"])
        blocked_end = slot.end + timedelta(minutes=type_row["buffer_after_min"])

        try:
            await conn.execute(
                """
                UPDATE appointments
                SET provider_id = $2, appointment_type_id = $3,
                    starts_at = $4, ends_at = $5,
                    buffer_before_min = $6, buffer_after_min = $7,
                    blocked_range = tstzrange($8, $9)
                WHERE id = $1
                """,
                appointment_id,
                slot.provider_id,
                slot.appointment_type_id,
                slot.start,
                slot.end,
                type_row["buffer_before_min"],
                type_row["buffer_after_min"],
                blocked_start,
                blocked_end,
            )
        except (
            asyncpg.exceptions.ExclusionViolationError,
            # Should be unreachable now that writes are serialized per provider, but
            # kept so a future write path that forgets the lock degrades into a clean
            # "slot taken" instead of surfacing a raw driver error to a caller.
            asyncpg.exceptions.DeadlockDetectedError,
        ) as exc:
            raise SlotTaken(
                f"{slot.start.isoformat()} is no longer available for provider {slot.provider_id}"
            ) from exc

        # The appointment moved, so any reminder scheduled for the old time is stale.
        await _reset_reminder(conn, appointment_id)

    return Booked(
        appointment_id=appointment_id,
        provider_id=slot.provider_id,
        appointment_type_id=slot.appointment_type_id,
        patient_id=existing["patient_id"],
        starts_at=slot.start,
        ends_at=slot.end,
    )


async def load_busy(
    pool: asyncpg.Pool,
    *,
    provider_ids: list[int],
    window_start: datetime,
    window_end: datetime,
) -> list[Busy]:
    """Existing appointments overlapping a window, shaped for find_slots.

    This is the only seam between the pure scheduler and the database.
    """
    rows = await pool.fetch(
        """
        SELECT provider_id, starts_at, ends_at, buffer_before_min, buffer_after_min
        FROM appointments
        WHERE status = 'booked'
          AND provider_id = ANY($1::int[])
          AND blocked_range && tstzrange($2, $3)
        ORDER BY starts_at
        """,
        provider_ids,
        window_start,
        window_end,
    )
    return [
        Busy(
            provider_id=r["provider_id"],
            start=r["starts_at"].astimezone(UTC),
            end=r["ends_at"].astimezone(UTC),
            buffer_before_min=r["buffer_before_min"],
            buffer_after_min=r["buffer_after_min"],
        )
        for r in rows
    ]


async def load_exceptions(
    pool: asyncpg.Pool,
    *,
    provider_ids: list[int],
    from_date: date,
    to_date: date,
) -> list[AvailabilityException]:
    """Dated availability overrides (closures, half-days) for a window.

    Recurring weekly hours come from config.yaml; only *exceptions* are runtime
    data, because that is what staff add day to day.
    """
    rows = await pool.fetch(
        """
        SELECT provider_id, on_date, is_closed, start_time, end_time
        FROM availability_exceptions
        WHERE provider_id = ANY($1::int[]) AND on_date BETWEEN $2 AND $3
        """,
        provider_ids,
        from_date,
        to_date,
    )
    return [
        AvailabilityException(
            provider_id=r["provider_id"],
            on_date=r["on_date"],
            is_closed=r["is_closed"],
            start=r["start_time"],
            end=r["end_time"],
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class UpcomingAppointment:
    appointment_id: int
    provider_id: int
    provider_name: str
    appointment_type_id: int
    appointment_type_name: str
    patient_name: str
    starts_at: datetime
    ends_at: datetime


async def find_upcoming_appointments(
    pool: asyncpg.Pool,
    *,
    phone: str | None = None,
    name: str | None = None,
    now: datetime | None = None,
    limit: int = 5,
) -> list[UpcomingAppointment]:
    """Booked, still-future appointments for a caller, soonest first.

    Caller ID is the usual way in, which is why phone is matched exactly and name only
    as a fallback -- asking someone to spell their name to a phone robot is exactly the
    experience this project is trying to avoid.
    """
    if not phone and not name:
        raise ValueError("find_upcoming_appointments requires phone or name")

    now = now or datetime.now(UTC)

    rows = await pool.fetch(
        """
        SELECT a.id, a.provider_id, p.name AS provider_name,
               a.appointment_type_id, t.name AS type_name,
               pt.name AS patient_name, a.starts_at, a.ends_at
        FROM appointments a
        JOIN providers p ON p.id = a.provider_id
        JOIN appointment_types t ON t.id = a.appointment_type_id
        JOIN patients pt ON pt.id = a.patient_id
        WHERE a.status = 'booked'
          AND a.starts_at >= $3
          AND (
                ($1::text IS NOT NULL AND pt.phone = $1)
             OR ($2::text IS NOT NULL AND lower(pt.name) = lower($2))
          )
        ORDER BY a.starts_at
        LIMIT $4
        """,
        phone,
        name,
        now,
        limit,
    )
    return [
        UpcomingAppointment(
            appointment_id=r["id"],
            provider_id=r["provider_id"],
            provider_name=r["provider_name"],
            appointment_type_id=r["appointment_type_id"],
            appointment_type_name=r["type_name"],
            patient_name=r["patient_name"],
            starts_at=r["starts_at"].astimezone(UTC),
            ends_at=r["ends_at"].astimezone(UTC),
        )
        for r in rows
    ]
