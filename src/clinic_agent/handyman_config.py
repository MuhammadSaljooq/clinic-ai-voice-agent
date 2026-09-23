"""Handyman-business configuration: everything specific to the Task Titan agent.

Parallels `config.py` (clinic) and `rental_config.py` (trailer) but for a third,
independent domain, so all three agents stay isolated. Validation is strict and happens
at load: a bad timezone or an empty service list fails on startup, not mid-call.

There is no inventory or pricing here -- a handyman quotes after seeing the job, so the
agent only ever offers a free estimate and records a request. That is why this config is
simpler than the trailer's (no rates, deposits, or units).
"""

from __future__ import annotations

import pathlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator


class BusinessInfo(BaseModel):
    name: str
    owner_name: str
    timezone: str
    service_area: str
    human_transfer_number: str
    phone_display: str
    email: str | None = None

    @field_validator("timezone")
    @classmethod
    def _resolvable(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class ServiceCategory(BaseModel):
    name: str
    examples: str | None = None


class FaqEntry(BaseModel):
    q: str
    a: str


class HandymanConfig(BaseModel):
    business: BusinessInfo
    services: list[ServiceCategory] = Field(min_length=1)
    faq: list[FaqEntry] = Field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business.timezone)


def load_handyman_config(path: str | pathlib.Path) -> HandymanConfig:
    raw = yaml.safe_load(pathlib.Path(path).read_text())
    return HandymanConfig.model_validate(raw)
