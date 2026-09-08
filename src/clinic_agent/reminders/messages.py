"""Compose reminder SMS text. Pure.

Two constraints shape every line here.

**Minimum necessary.** A reminder reveals that someone is a patient of a specific
clinic, which is already sensitive. It says *when* and *where to call*, and nothing
about why they are coming, which provider they are seeing, or what was discussed.
No specialty, no appointment type, no reason for visit.

**Length.** SMS bills per 160-character segment, and a single non-GSM character
(a curly quote, an en dash, an emoji) collapses the limit to 70 by forcing UCS-2.
`sms_segments` makes that visible so a wording change cannot silently double the bill.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

# Characters encodable in the GSM 03.38 7-bit alphabet. Anything outside this forces
# the whole message to UCS-2 and cuts the segment size from 160 to 70.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("^{}\\[~]|€")

SINGLE_SEGMENT_GSM7 = 160
MULTI_SEGMENT_GSM7 = 153
SINGLE_SEGMENT_UCS2 = 70
MULTI_SEGMENT_UCS2 = 67


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _clock(moment: datetime) -> str:
    minute = f":{moment.minute:02d}" if moment.minute else ""
    hour = moment.hour % 12 or 12
    return f"{hour}{minute}{'am' if moment.hour < 12 else 'pm'}"


def describe_when(starts_at: datetime, tz: ZoneInfo, today: date) -> str:
    """"today at 2:30pm", "tomorrow at 9am", or "Thu 17th at 9am"."""
    local = starts_at.astimezone(tz)
    days_away = (local.date() - today).days
    if days_away == 0:
        return f"today at {_clock(local)}"
    if days_away == 1:
        return f"tomorrow at {_clock(local)}"
    return f"{local:%a} {local.day}{_ordinal(local.day)} at {_clock(local)}"


def sms_segments(text: str) -> int:
    """How many billable segments this message costs."""
    if not text:
        return 0
    gsm7 = all(c in GSM7 or c in GSM7_EXTENDED for c in text)
    if gsm7:
        # Extended characters take two septets each.
        length = sum(2 if c in GSM7_EXTENDED else 1 for c in text)
    else:
        # UCS-2 counts UTF-16 code units, not Python characters. An emoji outside the
        # BMP is a surrogate pair and costs two units, so len() understates the bill.
        length = len(text.encode("utf-16-le")) // 2
    single = SINGLE_SEGMENT_GSM7 if gsm7 else SINGLE_SEGMENT_UCS2
    multi = MULTI_SEGMENT_GSM7 if gsm7 else MULTI_SEGMENT_UCS2
    if length <= single:
        return 1
    return -(-length // multi)  # ceiling division


def compose_reminder(
    *,
    clinic_name: str,
    phone_display: str,
    starts_at: datetime,
    tz: ZoneInfo,
    today: date,
) -> str:
    """The reminder a patient receives.

    Deliberately says nothing about why they are coming or who they are seeing.
    """
    when = describe_when(starts_at, tz, today)
    return (
        f"{clinic_name}: reminder of your appointment {when}. "
        f"Reply C to cancel, or call {phone_display}. Reply STOP to opt out."
    )


def compose_cancellation_confirmation(clinic_name: str, phone_display: str) -> str:
    return (
        f"{clinic_name}: your appointment is cancelled. "
        f"Call {phone_display} to rebook."
    )


def compose_optout_confirmation(clinic_name: str) -> str:
    return f"{clinic_name}: you will not receive any more texts from us. Reply START to resume."


def compose_optin_confirmation(clinic_name: str) -> str:
    return f"{clinic_name}: you will receive appointment reminders again. Reply STOP to opt out."
