"""Trailer-rental business configuration: everything specific to the rental agent.

Parallels `config.py` (the clinic config) but for a separate domain, so the two agents
stay fully isolated. Validation is strict and happens at load: a bad timezone, a
duplicate trailer-type id, or an impossible rental window should fail on startup, not
halfway through a call.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class BusinessInfo(BaseModel):
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


class TrailerTypeConfig(BaseModel):
    id: int
    name: str
    description: str | None = None
    daily_rate: Decimal = Field(gt=0)
    deposit: Decimal = Field(default=Decimal("0"), ge=0)
    units: int = Field(gt=0)


class RentalRules(BaseModel):
    min_days: int = Field(default=1, ge=1)
    max_days: int = Field(default=30, ge=1)
    min_lead_days: int = Field(default=0, ge=0)
    booking_horizon_days: int = Field(default=120, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> RentalRules:
        if self.min_days > self.max_days:
            raise ValueError(f"min_days {self.min_days} must be <= max_days {self.max_days}")
        return self


class FaqEntry(BaseModel):
    q: str
    a: str


class RentalConfig(BaseModel):
    business: BusinessInfo
    rental: RentalRules = Field(default_factory=RentalRules)
    trailer_types: list[TrailerTypeConfig] = Field(min_length=1)
    faq: list[FaqEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_types(self) -> RentalConfig:
        ids = [t.id for t in self.trailer_types]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate trailer type ids")
        names = [t.name for t in self.trailer_types]
        if len(set(names)) != len(names):
            raise ValueError("duplicate trailer type names")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business.timezone)

    def type_by_id(self, type_id: int) -> TrailerTypeConfig | None:
        return next((t for t in self.trailer_types if t.id == type_id), None)

    def type_by_name(self, name: str | None) -> TrailerTypeConfig | None:
        return next((t for t in self.trailer_types if t.name == name), None)


def load_rental_config(path: str | pathlib.Path) -> RentalConfig:
    raw = yaml.safe_load(pathlib.Path(path).read_text())
    return RentalConfig.model_validate(raw)
