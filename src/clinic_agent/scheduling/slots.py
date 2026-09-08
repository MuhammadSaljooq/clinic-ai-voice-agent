"""Pure slot generation. No database, no network, no clock reads.

Every input is passed in explicitly -- including ``now`` -- so the densest logic in
the system is exhaustively testable with no mocks and no patching.

DST correctness comes from *construction*, not special-casing: local working-hour
**endpoints** are converted to UTC and all stepping happens in UTC. A 09:00-17:00
local day is therefore 7 real hours on spring-forward and 9 on fall-back, with no
branch anywhere that mentions daylight saving.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from clinic_agent.scheduling.models import (
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
    Busy,
    Provider,
    Slot,
    SlotPolicy,
)

UTC = ZoneInfo("UTC")


def _require_aware(dt: datetime, label: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"{label} must be tz-aware; naive datetimes silently produce wrong slots"
        )


def _working_intervals(
    provider_id: int,
    on: date,
    provider_rules: list[AvailabilityRule],
    exceptions: dict[tuple[int, date], AvailabilityException],
) -> list[tuple[time, time]]:
    """Local working intervals for one provider on one date.

    An exception fully replaces that date's recurring rules (domain rule 7).
    """
    exc = exceptions.get((provider_id, on))
    if exc is not None:
        if exc.is_closed:
            return []
        assert exc.start is not None and exc.end is not None  # enforced in __post_init__
        return [(exc.start, exc.end)]
    return [(r.start, r.end) for r in provider_rules if r.weekday == on.weekday()]


def find_slots(
    *,
    appointment_type: AppointmentType,
    providers: list[Provider],
    rules: list[AvailabilityRule],
    exceptions: list[AvailabilityException],
    busy: list[Busy],
    policy: SlotPolicy,
    tz: ZoneInfo,
    now: datetime,
    search_from: date,
    search_to: date,
    limit: int = 3,
) -> list[Slot]:
    """Return bookable slots, earliest first.

    A candidate survives when all of the following hold:

    1. its body ``[start, start+duration]`` fits inside a working interval;
    2. its *buffered* region misses every existing appointment's buffered region;
    3. it is no sooner than ``now + min_lead_time`` and no later than
       ``now + booking_horizon_days``.

    Buffers deliberately may fall outside working hours -- they model provider
    turnaround, so a 30-minute pre-buffer must not veto the first slot of the day.
    """
    _require_aware(now, "now")
    if policy.granularity_min <= 0:
        raise ValueError(f"granularity_min must be positive, got {policy.granularity_min}")
    for b in busy:
        _require_aware(b.start, "busy.start")
        _require_aware(b.end, "busy.end")

    earliest = now + timedelta(minutes=policy.min_lead_time_min)
    latest = now + timedelta(days=policy.booking_horizon_days)

    duration = timedelta(minutes=appointment_type.duration_min)
    step = timedelta(minutes=policy.granularity_min)
    pad_before = timedelta(minutes=appointment_type.buffer_before_min)
    pad_after = timedelta(minutes=appointment_type.buffer_after_min)

    rules_by_provider: dict[int, list[AvailabilityRule]] = defaultdict(list)
    for r in rules:
        rules_by_provider[r.provider_id].append(r)

    exc_by_key = {(e.provider_id, e.on_date): e for e in exceptions}

    # Pre-widen existing appointments by their own buffers once, up front.
    blocked_by_provider: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for b in busy:
        blocked_by_provider[b.provider_id].append(
            (
                b.start - timedelta(minutes=b.buffer_before_min),
                b.end + timedelta(minutes=b.buffer_after_min),
            )
        )

    found: list[Slot] = []

    for provider in providers:
        provider_rules = rules_by_provider.get(provider.id, [])
        blocked = blocked_by_provider.get(provider.id, [])

        on = search_from
        while on <= search_to:
            for local_start, local_end in _working_intervals(
                provider.id, on, provider_rules, exc_by_key
            ):
                window_start = datetime.combine(on, local_start, tzinfo=tz).astimezone(UTC)
                window_end = datetime.combine(on, local_end, tzinfo=tz).astimezone(UTC)

                candidate = window_start
                while candidate + duration <= window_end:
                    if earliest <= candidate <= latest:
                        pad_start = candidate - pad_before
                        pad_end = candidate + duration + pad_after
                        # Strict overlap: touching endpoints are adjacent, not conflicting.
                        conflicts = any(
                            pad_start < busy_end and busy_start < pad_end
                            for busy_start, busy_end in blocked
                        )
                        if not conflicts:
                            found.append(
                                Slot(
                                    provider_id=provider.id,
                                    appointment_type_id=appointment_type.id,
                                    start=candidate,
                                    end=candidate + duration,
                                )
                            )
                    candidate += step
            on += timedelta(days=1)

    found.sort(key=lambda s: (s.start, s.provider_id))
    return found[:limit]
