"""Frozen value objects for scheduling. No behaviour, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True, slots=True)
class Provider:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class AppointmentType:
    id: int
    name: str
    duration_min: int
    buffer_before_min: int = 0
    buffer_after_min: int = 0

    def __post_init__(self) -> None:
        if self.duration_min <= 0:
            raise ValueError(f"duration_min must be positive, got {self.duration_min}")
        if self.buffer_before_min < 0 or self.buffer_after_min < 0:
            raise ValueError("buffers must be non-negative")


@dataclass(frozen=True, slots=True)
class AvailabilityRule:
    """Recurring weekly availability in clinic-local time. weekday 0=Mon .. 6=Sun."""

    provider_id: int
    weekday: int
    start: time
    end: time

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday must be 0..6, got {self.weekday}")
        if self.start >= self.end:
            raise ValueError(f"start {self.start} must be before end {self.end}")


@dataclass(frozen=True, slots=True)
class AvailabilityException:
    """Dated override. is_closed removes the day; start/end replace that day's rules."""

    provider_id: int
    on_date: date
    is_closed: bool = False
    start: time | None = None
    end: time | None = None

    def __post_init__(self) -> None:
        if self.is_closed:
            return
        if self.start is None or self.end is None:
            raise ValueError("non-closed exception requires both start and end")
        if self.start >= self.end:
            raise ValueError(f"start {self.start} must be before end {self.end}")


@dataclass(frozen=True, slots=True)
class Busy:
    """An existing appointment occupying a provider's time. Must be tz-aware UTC."""

    provider_id: int
    start: datetime
    end: datetime
    buffer_before_min: int = 0
    buffer_after_min: int = 0


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    granularity_min: int = 15
    min_lead_time_min: int = 120
    booking_horizon_days: int = 60


@dataclass(frozen=True, slots=True)
class Slot:
    provider_id: int
    appointment_type_id: int
    start: datetime
    end: datetime
