"""Table-driven tests for pure slot generation.

Clinic timezone is America/New_York throughout. September dates are EDT (UTC-4),
so 09:00 local == 13:00 UTC. Helpers convert explicitly so the intent stays readable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from clinic_agent.scheduling.models import (
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
    Busy,
    Provider,
    SlotPolicy,
)
from clinic_agent.scheduling.slots import find_slots

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MONDAY = date(2026, 9, 14)  # a Monday, EDT
SATURDAY = date(2026, 9, 19)


def local_utc(d: date, h: int, m: int = 0) -> datetime:
    """A clinic-local wall time expressed as the UTC instant it maps to."""
    return datetime(d.year, d.month, d.day, h, m, tzinfo=NY).astimezone(UTC)


def wall(dt: datetime) -> tuple[int, int]:
    """Local wall-clock (hour, minute) of a UTC instant."""
    loc = dt.astimezone(NY)
    return (loc.hour, loc.minute)


DR = Provider(1, "Dr. Reyes")
DR2 = Provider(2, "Dr. Osei")

WEEKDAY_9_TO_5 = [
    AvailabilityRule(provider_id=1, weekday=wd, start=time(9), end=time(17))
    for wd in range(5)
]

FOLLOW_UP = AppointmentType(1, "Follow-up", duration_min=30)
NEW_PATIENT = AppointmentType(2, "New patient", duration_min=60)
POLICY = SlotPolicy(granularity_min=15, min_lead_time_min=120, booking_horizon_days=60)

# 04:00 local on the search day -> lead time of 120min lands at 06:00, before opening,
# so the whole working day is eligible.
EARLY_NOW = local_utc(MONDAY, 4)


def call(
    *,
    appointment_type=FOLLOW_UP,
    providers=(DR,),
    rules=None,
    exceptions=(),
    busy=(),
    policy=POLICY,
    now=EARLY_NOW,
    search_from=MONDAY,
    search_to=MONDAY,
    limit=100,
):
    return find_slots(
        appointment_type=appointment_type,
        providers=list(providers),
        rules=list(WEEKDAY_9_TO_5 if rules is None else rules),
        exceptions=list(exceptions),
        busy=list(busy),
        policy=policy,
        tz=NY,
        now=now,
        search_from=search_from,
        search_to=search_to,
        limit=limit,
    )


def test_basic_grid_starts_at_opening_and_steps_by_granularity():
    slots = call()
    assert wall(slots[0].start) == (9, 0)
    assert wall(slots[1].start) == (9, 15)
    assert wall(slots[2].start) == (9, 30)


def test_last_start_leaves_room_for_full_duration():
    """A 60-min appointment in a 9-17 day cannot start at 16:15."""
    slots = call(appointment_type=NEW_PATIENT)
    assert wall(slots[-1].start) == (16, 0)
    assert slots[-1].end == local_utc(MONDAY, 17)


def test_slot_count_matches_grid_arithmetic():
    # 9:00..16:30 inclusive at 15-min steps for a 30-min appointment = 31 slots
    slots = call()
    assert len(slots) == 31


def test_existing_appointment_blocks_overlapping_candidates():
    busy = [
        Busy(
            provider_id=1,
            start=local_utc(MONDAY, 11),
            end=local_utc(MONDAY, 11, 30),
            buffer_after_min=15,
        )
    ]
    starts = {wall(s.start) for s in call(busy=busy)}
    assert (11, 0) not in starts, "overlaps the appointment itself"
    assert (11, 30) not in starts, "overlaps the 15-min after-buffer"
    assert (11, 45) in starts, "clear of the buffer"


def test_adjacent_booking_is_allowed_not_treated_as_overlap():
    """A slot ending exactly when another starts must remain bookable."""
    busy = [Busy(provider_id=1, start=local_utc(MONDAY, 11), end=local_utc(MONDAY, 11, 30))]
    starts = {wall(s.start) for s in call(busy=busy)}
    assert (10, 30) in starts, "ends at 11:00 exactly, touching is not overlapping"


def test_buffer_before_does_not_block_first_slot_of_day():
    """Domain rule 2: buffers are provider turnaround, they need not fit in working hours."""
    padded = AppointmentType(3, "Procedure", duration_min=30, buffer_before_min=30)
    starts = {wall(s.start) for s in call(appointment_type=padded)}
    assert (9, 0) in starts


def test_closure_removes_the_whole_day():
    exc = [AvailabilityException(provider_id=1, on_date=MONDAY, is_closed=True)]
    assert call(exceptions=exc) == []


def test_exception_with_hours_replaces_the_days_rules():
    exc = [
        AvailabilityException(
            provider_id=1, on_date=MONDAY, start=time(13), end=time(15)
        )
    ]
    slots = call(appointment_type=NEW_PATIENT, exceptions=exc)
    assert wall(slots[0].start) == (13, 0)
    assert wall(slots[-1].start) == (14, 0)


def test_min_lead_time_excludes_slots_that_are_too_soon():
    # 08:30 local + 120 min lead => nothing before 10:30
    slots = call(now=local_utc(MONDAY, 8, 30))
    assert wall(slots[0].start) == (10, 30)


def test_booking_horizon_excludes_far_future_slots():
    far = MONDAY + timedelta(days=30)
    slots = call(
        policy=SlotPolicy(granularity_min=15, min_lead_time_min=120, booking_horizon_days=1),
        search_from=far,
        search_to=far,
    )
    assert slots == []


def test_weekend_has_no_availability():
    assert call(search_from=SATURDAY, search_to=SATURDAY) == []


def test_multiple_providers_are_merged_and_sorted_by_start():
    rules = WEEKDAY_9_TO_5 + [
        AvailabilityRule(provider_id=2, weekday=0, start=time(9), end=time(17))
    ]
    slots = call(providers=(DR, DR2), rules=rules)
    assert {s.provider_id for s in slots} == {1, 2}
    assert slots == sorted(slots, key=lambda s: (s.start, s.provider_id))


def test_busy_for_one_provider_does_not_block_another():
    rules = WEEKDAY_9_TO_5 + [
        AvailabilityRule(provider_id=2, weekday=0, start=time(9), end=time(17))
    ]
    busy = [Busy(provider_id=1, start=local_utc(MONDAY, 11), end=local_utc(MONDAY, 11, 30))]
    slots = call(providers=(DR, DR2), rules=rules, busy=busy)
    at_11 = {s.provider_id for s in slots if wall(s.start) == (11, 0)}
    assert at_11 == {2}


def test_limit_truncates_results():
    assert len(call(limit=3)) == 3


def test_naive_busy_datetime_is_rejected():
    """Silently mixing naive and aware datetimes is how timezone bugs get shipped."""
    busy = [Busy(provider_id=1, start=datetime(2026, 9, 14, 15), end=datetime(2026, 9, 14, 16))]
    with pytest.raises(ValueError, match="tz-aware"):
        call(busy=busy)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="tz-aware"):
        call(now=datetime(2026, 9, 14, 8))
