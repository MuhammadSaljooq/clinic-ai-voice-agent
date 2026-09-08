"""The scheduling API the voice agent calls.

Joins the three sources the pure scheduler needs -- recurring rules from config,
dated exceptions and existing appointments from the database -- and hands back
signed slot tokens, so a slot the agent offers is the only thing it can book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import asyncpg

from clinic_agent.config import ClinicConfig
from clinic_agent.scheduling.booking import (
    ProviderCannotPerformType,
    load_busy,
    load_exceptions,
)
from clinic_agent.scheduling.models import Slot
from clinic_agent.scheduling.slots import find_slots
from clinic_agent.scheduling.tokens import issue_slot_token

DEFAULT_SEARCH_DAYS = 14

# Fixed and explicit so tests are unambiguous and the agent's promises match reality.
PART_OF_DAY_HOURS: dict[str, tuple[int, int]] = {
    "morning": (0, 12),
    "afternoon": (12, 17),
    "evening": (17, 24),
}

# find_slots is asked for more than we will offer, because preference filtering happens
# afterwards -- filtering a list that was already truncated to three would silently
# return nothing whenever the first three slots fell outside the requested window.
_PREFILTER_LIMIT = 500


@dataclass(frozen=True, slots=True)
class OfferedSlot:
    """A slot plus the token needed to book it."""

    slot: Slot
    token: str
    provider_name: str

    def spoken_time(self, cfg: ClinicConfig) -> str:
        """How the agent should say this slot out loud, in clinic-local time."""
        local = self.slot.start.astimezone(cfg.tz)
        minute = f":{local.minute:02d}" if local.minute else ""
        hour = local.hour % 12 or 12
        meridiem = "am" if local.hour < 12 else "pm"
        return f"{local:%A} the {local.day}{_ordinal(local.day)} at {hour}{minute} {meridiem}"


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


async def available_slots(
    pool: asyncpg.Pool,
    cfg: ClinicConfig,
    secret: str,
    *,
    appointment_type_id: int,
    provider_id: int | None = None,
    earliest_date: date | None = None,
    part_of_day: str | None = None,
    search_days: int = DEFAULT_SEARCH_DAYS,
    now: datetime | None = None,
    limit: int = 3,
) -> list[OfferedSlot]:
    now = now or datetime.now(UTC)

    if part_of_day is not None and part_of_day not in PART_OF_DAY_HOURS:
        raise ValueError(
            f"unknown part_of_day {part_of_day!r}; expected one of {sorted(PART_OF_DAY_HOURS)}"
        )

    appointment_type = cfg.domain_appointment_type(appointment_type_id)
    providers = cfg.providers_for_type(appointment_type_id)

    if provider_id is not None:
        providers = [p for p in providers if p.id == provider_id]
        if not providers:
            raise ProviderCannotPerformType(
                f"provider {provider_id} does not perform appointment type {appointment_type_id}"
            )

    provider_ids = [p.id for p in providers]
    # Search in clinic-local dates: patients think in local days, not UTC days.
    today = now.astimezone(cfg.tz).date()
    # A caller asking for a date in the past means they misspoke or the model
    # miscalculated; clamp rather than return an empty list they cannot act on.
    search_from = max(earliest_date, today) if earliest_date else today
    search_to = search_from + timedelta(days=search_days)

    busy = await load_busy(
        pool,
        provider_ids=provider_ids,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=search_days + 2 + (search_from - today).days),
    )
    exceptions = await load_exceptions(
        pool, provider_ids=provider_ids, from_date=search_from, to_date=search_to
    )

    slots = find_slots(
        appointment_type=appointment_type,
        providers=providers,
        rules=cfg.domain_rules(),
        exceptions=exceptions,
        busy=busy,
        policy=cfg.domain_slot_policy(),
        tz=cfg.tz,
        now=now,
        search_from=search_from,
        search_to=search_to,
        limit=_PREFILTER_LIMIT,
    )

    if part_of_day is not None:
        start_hour, end_hour = PART_OF_DAY_HOURS[part_of_day]
        slots = [
            s for s in slots if start_hour <= s.start.astimezone(cfg.tz).hour < end_hour
        ]

    slots = slots[:limit]

    names = {p.id: p.name for p in providers}
    return [
        OfferedSlot(
            slot=s,
            token=issue_slot_token(s, secret, now=now),
            provider_name=names[s.provider_id],
        )
        for s in slots
    ]
