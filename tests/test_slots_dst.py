"""DST correctness.

US transitions in 2026: spring forward 2026-03-08 (02:00 EST -> 03:00 EDT),
fall back 2026-11-01 (02:00 EDT -> 01:00 EST). Both land on a Sunday at 02:00
local, so an ordinary 09:00-17:00 clinic day does NOT span the transition --
it is still 8 real hours. What *does* change is which UTC instant 09:00 local
maps to, and any window that actually crosses 02:00 gains or loses an hour.

These tests pin both behaviours.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from clinic_agent.scheduling.models import (
    AppointmentType,
    AvailabilityRule,
    Provider,
    SlotPolicy,
)
from clinic_agent.scheduling.slots import find_slots

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

SPRING_FORWARD = date(2026, 3, 8)  # Sunday; 02:00 EST -> 03:00 EDT (hour vanishes)
DAY_BEFORE_SPRING = date(2026, 3, 7)  # Saturday, still EST
FALL_BACK = date(2026, 11, 1)  # Sunday; 02:00 EDT -> 01:00 EST (hour repeats)

DR = Provider(1, "Dr. Reyes")
HOUR_LONG = AppointmentType(1, "Consult", duration_min=60)
HOURLY = SlotPolicy(granularity_min=60, min_lead_time_min=0, booking_horizon_days=400)


def run(rules, on, now):
    return find_slots(
        appointment_type=HOUR_LONG,
        providers=[DR],
        rules=rules,
        exceptions=[],
        busy=[],
        policy=HOURLY,
        tz=NY,
        now=now,
        search_from=on,
        search_to=on,
        limit=100,
    )


def walls(slots):
    return [(s.start.astimezone(NY).hour, s.start.astimezone(NY).minute) for s in slots]


def test_same_local_opening_maps_to_different_utc_across_the_transition():
    """09:00 local is 14:00 UTC under EST but 13:00 UTC under EDT.

    An implementation that converted once and added 24h per day would report
    14:00 UTC on both days and be an hour wrong for eight months of the year.
    """
    weekend_rules = [
        AvailabilityRule(provider_id=1, weekday=5, start=time(9), end=time(17)),  # Sat
        AvailabilityRule(provider_id=1, weekday=6, start=time(9), end=time(17)),  # Sun
    ]
    now = datetime(2026, 3, 1, tzinfo=UTC)

    saturday = run(weekend_rules, DAY_BEFORE_SPRING, now)
    sunday = run(weekend_rules, SPRING_FORWARD, now)

    assert saturday[0].start == datetime(2026, 3, 7, 14, 0, tzinfo=UTC), "EST is UTC-5"
    assert sunday[0].start == datetime(2026, 3, 8, 13, 0, tzinfo=UTC), "EDT is UTC-4"
    # Wall-clock opening time is identical on both days.
    assert walls(saturday)[0] == walls(sunday)[0] == (9, 0)


def test_ordinary_clinic_day_is_unaffected_by_the_transition():
    """The 02:00 transition is outside 09:00-17:00, so slot count is unchanged."""
    weekend_rules = [
        AvailabilityRule(provider_id=1, weekday=5, start=time(9), end=time(17)),
        AvailabilityRule(provider_id=1, weekday=6, start=time(9), end=time(17)),
    ]
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert len(run(weekend_rules, DAY_BEFORE_SPRING, now)) == 8
    assert len(run(weekend_rules, SPRING_FORWARD, now)) == 8


def test_window_spanning_spring_forward_loses_an_hour():
    """01:00-05:00 local reads as 4 hours but is only 3 real hours."""
    overnight = [AvailabilityRule(provider_id=1, weekday=6, start=time(1), end=time(5))]
    slots = run(overnight, SPRING_FORWARD, datetime(2026, 3, 1, tzinfo=UTC))

    assert len(slots) == 3, "a naive local-arithmetic implementation would say 4"
    # 02:00 local never happens: wall times step 01:00 -> 03:00.
    assert walls(slots) == [(1, 0), (3, 0), (4, 0)]


def test_window_spanning_fall_back_gains_an_hour():
    """01:00-03:00 local reads as 2 hours but is 3 real hours."""
    overnight = [AvailabilityRule(provider_id=1, weekday=6, start=time(1), end=time(3))]
    slots = run(overnight, FALL_BACK, datetime(2026, 10, 25, tzinfo=UTC))

    assert len(slots) == 3, "a naive local-arithmetic implementation would say 2"
    # 01:00 local happens twice; both are real, distinct, bookable instants.
    assert walls(slots) == [(1, 0), (1, 0), (2, 0)]
    assert len({s.start for s in slots}) == 3, "distinct UTC instants despite repeated wall time"
