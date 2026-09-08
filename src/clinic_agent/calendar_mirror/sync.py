"""Mirror appointments into the clinic's Google Calendar.

**The mirror never breaks a booking.** Every method swallows and logs failures rather
than propagating them: a missing calendar entry is an annoyance, an appointment that
failed to save because Google was slow is not acceptable. Failures leave
`gcal_event_id` unset, so a later reconciliation pass can find and fix them.

Authentication is a **service account** the clinic shares its calendar with. That
avoids OAuth app verification entirely -- no consent screen, no review, no 100-test-user
cap.

The Google client is blocking, so calls run in a worker thread. It sits behind a small
protocol so all of this is testable without credentials.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import asyncpg

from clinic_agent.calendar_mirror.events import event_body
from clinic_agent.config import ClinicConfig

log = logging.getLogger(__name__)

CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)


class CalendarBackend(Protocol):
    """The three operations the mirror needs. Blocking; called via a thread."""

    def insert(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def patch(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def delete(self, calendar_id: str, event_id: str) -> None: ...


class GoogleCalendarBackend:
    """Adapter over googleapiclient, authenticated with a service account."""

    def __init__(self, service_account_info: dict[str, Any]):
        from google.oauth2 import service_account  # imported lazily: only needed live
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=list(CALENDAR_SCOPES)
        )
        self._events = build("calendar", "v3", credentials=credentials).events()

    def insert(self, calendar_id, body):
        return self._events.insert(calendarId=calendar_id, body=body).execute()

    def patch(self, calendar_id, event_id, body):
        return self._events.patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def delete(self, calendar_id, event_id):
        self._events.delete(calendarId=calendar_id, eventId=event_id).execute()


APPOINTMENT_QUERY = """
    SELECT a.id, a.starts_at, a.ends_at, a.gcal_event_id,
           pt.name AS patient_name, pt.phone AS patient_phone,
           p.name AS provider_name, t.name AS type_name
    FROM appointments a
    JOIN patients pt ON pt.id = a.patient_id
    JOIN providers p ON p.id = a.provider_id
    JOIN appointment_types t ON t.id = a.appointment_type_id
    WHERE a.id = $1
"""


class CalendarMirror:
    def __init__(self, backend: CalendarBackend, calendar_id: str, cfg: ClinicConfig):
        self._backend = backend
        self._calendar_id = calendar_id
        self._cfg = cfg

    async def _body_for(self, pool: asyncpg.Pool, appointment_id: int):
        row = await pool.fetchrow(APPOINTMENT_QUERY, appointment_id)
        if row is None:
            return None, None
        body = event_body(
            appointment_id=row["id"],
            patient_name=row["patient_name"],
            patient_phone=row["patient_phone"],
            provider_name=row["provider_name"],
            appointment_type_name=row["type_name"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            tz=self._cfg.tz,
        )
        return row, body

    async def on_booked(self, pool: asyncpg.Pool, appointment_id: int) -> str | None:
        row, body = await self._body_for(pool, appointment_id)
        if row is None:
            return None
        try:
            created = await asyncio.to_thread(self._backend.insert, self._calendar_id, body)
        except Exception:
            log.exception("calendar mirror: could not create event for %s", appointment_id)
            return None
        event_id = (created or {}).get("id")
        if event_id:
            await pool.execute(
                "UPDATE appointments SET gcal_event_id = $2 WHERE id = $1",
                appointment_id, event_id,
            )
        return event_id

    async def on_rescheduled(self, pool: asyncpg.Pool, appointment_id: int) -> str | None:
        row, body = await self._body_for(pool, appointment_id)
        if row is None:
            return None
        # No mirrored event yet (a booking whose mirror failed): create rather than
        # silently doing nothing, so a reschedule repairs the gap.
        if not row["gcal_event_id"]:
            return await self.on_booked(pool, appointment_id)
        try:
            await asyncio.to_thread(
                self._backend.patch, self._calendar_id, row["gcal_event_id"], body
            )
        except Exception:
            log.exception("calendar mirror: could not update event for %s", appointment_id)
            return None
        return row["gcal_event_id"]

    async def on_cancelled(self, pool: asyncpg.Pool, appointment_id: int) -> None:
        """Delete, not mark. A cancelled appointment still showing on a provider's
        phone is worse than no entry at all."""
        event_id = await pool.fetchval(
            "SELECT gcal_event_id FROM appointments WHERE id = $1", appointment_id
        )
        if not event_id:
            return  # never mirrored; nothing to remove
        try:
            await asyncio.to_thread(self._backend.delete, self._calendar_id, event_id)
        except Exception:
            log.exception("calendar mirror: could not delete event for %s", appointment_id)
            return
        await pool.execute(
            "UPDATE appointments SET gcal_event_id = NULL WHERE id = $1", appointment_id
        )
