"""Turn an appointment into a Google Calendar event body. Pure.

The clinic's own calendar is an internal staff view, so it carries more than an SMS
would: the appointment type is included because staff need it to prepare the room and
the time. What it never carries is free-text clinical detail -- and the system does not
collect a reason for visit at all, so there is none to leak.

The appointment id goes into `extendedProperties.private` rather than only the
description, so the mirror can be reconciled by query instead of by parsing prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

APPOINTMENT_ID_KEY = "clinic_appointment_id"
SOURCE_KEY = "clinic_source"
SOURCE_VALUE = "voice_agent"


def event_body(
    *,
    appointment_id: int,
    patient_name: str,
    patient_phone: str,
    provider_name: str,
    appointment_type_name: str,
    starts_at: datetime,
    ends_at: datetime,
    tz: ZoneInfo,
) -> dict[str, Any]:
    return {
        "summary": f"{patient_name} - {appointment_type_name}",
        "description": (
            f"Booked by the phone assistant.\n"
            f"Provider: {provider_name}\n"
            f"Patient phone: {patient_phone}\n"
            f"Reference: #{appointment_id}"
        ),
        # dateTime carries its own offset; timeZone makes the intent explicit so the
        # event still reads correctly if the calendar's own timezone differs.
        "start": {"dateTime": starts_at.astimezone(tz).isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": ends_at.astimezone(tz).isoformat(), "timeZone": str(tz)},
        "extendedProperties": {
            "private": {
                APPOINTMENT_ID_KEY: str(appointment_id),
                SOURCE_KEY: SOURCE_VALUE,
            }
        },
    }
