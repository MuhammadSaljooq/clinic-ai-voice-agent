"""Clinic configuration: everything business-specific lives in config.yaml.

Validation is deliberately strict and happens at load time. A typo in a timezone or
a provider id that does not exist should fail on startup, not halfway through a
phone call with a patient on the line.
"""

from __future__ import annotations

import pathlib
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from clinic_agent.scheduling.models import (
    AppointmentType,
    AvailabilityRule,
    Provider,
    SlotPolicy,
)

WEEKDAYS: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


class AvailabilityWindow(BaseModel):
    weekday: str
    start: time
    end: time

    @field_validator("weekday")
    @classmethod
    def _known_weekday(cls, v: str) -> str:
        key = v.strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ValueError(f"unknown weekday {v!r}; expected one of {sorted(WEEKDAYS)}")
        return key

    @model_validator(mode="after")
    def _ordered(self) -> AvailabilityWindow:
        if self.start >= self.end:
            raise ValueError(f"start {self.start} must be before end {self.end}")
        return self

    @property
    def weekday_index(self) -> int:
        return WEEKDAYS[self.weekday]


class ProviderConfig(BaseModel):
    id: int
    name: str
    availability: list[AvailabilityWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_overlapping_windows(self) -> ProviderConfig:
        by_day: dict[int, list[AvailabilityWindow]] = {}
        for window in self.availability:
            by_day.setdefault(window.weekday_index, []).append(window)
        for day, windows in by_day.items():
            ordered = sorted(windows, key=lambda w: w.start)
            for earlier, later in zip(ordered, ordered[1:], strict=False):
                if later.start < earlier.end:
                    raise ValueError(
                        f"provider {self.name!r} has overlapping availability on "
                        f"weekday {day}: {earlier.start}-{earlier.end} and "
                        f"{later.start}-{later.end}"
                    )
        return self


class AppointmentTypeConfig(BaseModel):
    id: int
    name: str
    duration_min: int = Field(gt=0)
    buffer_before_min: int = Field(default=0, ge=0)
    buffer_after_min: int = Field(default=0, ge=0)
    description: str | None = None
    providers: list[int] = Field(min_length=1)


class ClinicInfo(BaseModel):
    name: str
    timezone: str
    human_transfer_number: str
    phone_display: str

    @field_validator("timezone")
    @classmethod
    def _resolvable(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class SlotPolicyConfig(BaseModel):
    granularity_min: int = Field(default=15, gt=0)
    min_lead_time_min: int = Field(default=120, ge=0)
    booking_horizon_days: int = Field(default=60, gt=0)


class FaqEntry(BaseModel):
    q: str
    a: str


class ClinicConfig(BaseModel):
    clinic: ClinicInfo
    slot_policy: SlotPolicyConfig = Field(default_factory=SlotPolicyConfig)
    providers: list[ProviderConfig] = Field(min_length=1)
    appointment_types: list[AppointmentTypeConfig] = Field(min_length=1)
    faq: list[FaqEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> ClinicConfig:
        provider_ids = [p.id for p in self.providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("duplicate provider ids")

        type_ids = [t.id for t in self.appointment_types]
        if len(set(type_ids)) != len(type_ids):
            raise ValueError("duplicate appointment type ids")

        known = set(provider_ids)
        for appointment_type in self.appointment_types:
            unknown = sorted(set(appointment_type.providers) - known)
            if unknown:
                raise ValueError(
                    f"appointment type {appointment_type.name!r} references "
                    f"unknown provider ids {unknown}"
                )
        return self

    # --- projections into the pure domain model ---

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic.timezone)

    def domain_providers(self) -> list[Provider]:
        return [Provider(id=p.id, name=p.name) for p in self.providers]

    def domain_rules(self) -> list[AvailabilityRule]:
        return [
            AvailabilityRule(
                provider_id=p.id,
                weekday=w.weekday_index,
                start=w.start,
                end=w.end,
            )
            for p in self.providers
            for w in p.availability
        ]

    def domain_appointment_type(self, type_id: int) -> AppointmentType:
        for t in self.appointment_types:
            if t.id == type_id:
                return AppointmentType(
                    id=t.id,
                    name=t.name,
                    duration_min=t.duration_min,
                    buffer_before_min=t.buffer_before_min,
                    buffer_after_min=t.buffer_after_min,
                )
        raise KeyError(f"no appointment type with id {type_id}")

    def providers_for_type(self, type_id: int) -> list[Provider]:
        allowed = next(t.providers for t in self.appointment_types if t.id == type_id)
        return [p for p in self.domain_providers() if p.id in set(allowed)]

    def domain_slot_policy(self) -> SlotPolicy:
        return SlotPolicy(
            granularity_min=self.slot_policy.granularity_min,
            min_lead_time_min=self.slot_policy.min_lead_time_min,
            booking_horizon_days=self.slot_policy.booking_horizon_days,
        )


def load_config(path: str | pathlib.Path) -> ClinicConfig:
    raw = yaml.safe_load(pathlib.Path(path).read_text())
    return ClinicConfig.model_validate(raw)
