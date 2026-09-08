"""Reminder text: minimum necessary information, and predictable cost."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from clinic_agent.reminders.messages import (
    compose_optout_confirmation,
    compose_reminder,
    describe_when,
    sms_segments,
)

NY = ZoneInfo("America/New_York")
CLINIC = "Northside Family Clinic"
PHONE = "(555) 123-4567"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=NY).astimezone(UTC)


def reminder(day: int, hour: int, minute: int = 0, *, today_day: int = 14) -> str:
    return compose_reminder(
        clinic_name=CLINIC, phone_display=PHONE,
        starts_at=at(day, hour, minute), tz=NY, today=date(2026, 9, today_day),
    )


def test_reminder_names_the_clinic_the_time_and_the_optout():
    text = reminder(15, 14, 30)
    assert CLINIC in text
    assert "2:30pm" in text
    assert "STOP" in text
    assert PHONE in text


def test_reminder_says_nothing_about_why_they_are_coming():
    """A reminder already reveals someone is a patient. It should reveal nothing more."""
    text = reminder(15, 9).lower()
    for leaked in ["follow-up", "new patient", "procedure", "dr.", "reyes", "osei",
                   "symptom", "results", "diagnosis"]:
        assert leaked not in text, f"reminder leaked {leaked!r}"


@pytest.mark.parametrize(
    ("day", "hour", "minute", "expected"),
    [
        (14, 14, 30, "today at 2:30pm"),
        (15, 9, 0, "tomorrow at 9am"),
        (17, 9, 15, "Thu 17th at 9:15am"),
        (15, 12, 0, "tomorrow at 12pm"),
        (15, 0, 30, "tomorrow at 12:30am"),
    ],
)
def test_when_reads_the_way_people_talk(day, hour, minute, expected):
    assert describe_when(at(day, hour, minute), NY, date(2026, 9, 14)) == expected


@pytest.mark.parametrize(("day", "suffix"), [(1, "1st"), (2, "2nd"), (3, "3rd"),
                                             (11, "11th"), (12, "12th"), (13, "13th"),
                                             (21, "21st"), (22, "22nd")])
def test_ordinals_are_correct(day, suffix):
    when = describe_when(at(day, 10), NY, date(2026, 9, 5) if day > 5 else date(2026, 8, 20))
    assert suffix in when or "today" in when or "tomorrow" in when


def test_reminder_fits_in_one_sms_segment():
    """Two segments doubles the per-message cost for no benefit."""
    text = reminder(15, 14, 30)
    assert sms_segments(text) == 1, f"{len(text)} chars: {text}"


def test_reminder_uses_only_gsm7_characters():
    """One curly quote or en dash forces UCS-2 and cuts the limit from 160 to 70."""
    from clinic_agent.reminders.messages import GSM7, GSM7_EXTENDED
    text = reminder(15, 14, 30)
    offenders = [c for c in text if c not in GSM7 and c not in GSM7_EXTENDED]
    assert offenders == [], f"non-GSM7 characters would double the cost: {offenders}"


def test_segment_counting_reflects_the_encoding_cliff():
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    # A single emoji forces UCS-2, where the limit is 70.
    assert sms_segments("a" * 70) == 1
    assert sms_segments("a" * 69 + "😀") == 2


def test_optout_confirmation_explains_how_to_come_back():
    text = compose_optout_confirmation(CLINIC)
    assert "START" in text
    assert sms_segments(text) == 1


def test_non_bmp_characters_count_as_two_ucs2_units():
    """An emoji outside the BMP is a UTF-16 surrogate pair. Counting Python characters
    understates the bill, which is how an SMS budget quietly doubles."""
    assert sms_segments("😀") == 1
    assert sms_segments("😀" * 35) == 1, "35 emoji = 70 UTF-16 units"
    assert sms_segments("😀" * 36) == 2, "36 emoji = 72 units, over the 70 limit"
