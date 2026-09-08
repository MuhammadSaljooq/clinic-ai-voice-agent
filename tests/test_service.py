"""End-to-end scheduling: config -> database -> offered slots -> booking.

Runs against the real config.yaml and a real Postgres, so it exercises the whole
scheduling core the way the voice agent will.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.scheduling.booking import ProviderCannotPerformType, book
from clinic_agent.scheduling.service import available_slots

REPO = pathlib.Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
SECRET = "service-test-secret"

FOLLOW_UP = 1  # 15 min, 5 min after-buffer, Dr. Reyes + Dr. Osei
PROCEDURE = 3  # 60 min, Dr. Reyes only

# Monday 04:00 clinic-local. Lead time is 120 min, so the whole day is open.
MONDAY_4AM = datetime(2026, 9, 14, 4, 0, tzinfo=NY).astimezone(UTC)


@pytest.fixture
async def configured(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    return pool, cfg


async def offer(pool, cfg, **kw):
    return await available_slots(pool, cfg, SECRET, now=MONDAY_4AM, **kw)


async def test_slots_are_offered_from_the_shipped_config(configured):
    pool, cfg = configured
    offers = await offer(pool, cfg, appointment_type_id=FOLLOW_UP, limit=3)

    assert len(offers) == 3
    first = offers[0].slot.start.astimezone(NY)
    assert (first.hour, first.minute) == (9, 0), "Dr. Reyes opens at 09:00 on Mondays"
    assert offers[0].provider_name == "Dr. Reyes"
    assert all(o.token for o in offers), "every offer must carry a bookable token"


async def test_booking_an_offered_slot_removes_it_from_later_offers(configured):
    """The core loop: offer, book, and the slot is gone -- including its buffer."""
    pool, cfg = configured
    first_offer = (await offer(pool, cfg, appointment_type_id=FOLLOW_UP, limit=1))[0]

    await book(
        pool,
        slot_token=first_offer.token,
        secret=SECRET,
        patient_name="Ada Lovelace",
        patient_phone="+15550100",
        now=MONDAY_4AM,
    )

    after = await offer(pool, cfg, appointment_type_id=FOLLOW_UP, limit=1)
    new_first = after[0].slot.start.astimezone(NY)

    # 09:00-09:15 plus a 5-min buffer blocks through 09:20, so 09:15 is unavailable
    # and the next slot on the 15-minute grid is 09:30.
    assert (new_first.hour, new_first.minute) == (9, 30)


async def test_provider_filter_respects_who_offers_the_type(configured):
    pool, cfg = configured
    offers = await offer(pool, cfg, appointment_type_id=FOLLOW_UP, provider_id=2, limit=1)
    assert offers[0].provider_name == "Dr. Osei"
    first = offers[0].slot.start.astimezone(NY)
    assert (first.hour, first.minute) == (12, 0), "Dr. Osei starts at 12:00 on Mondays"


async def test_requesting_a_provider_who_does_not_offer_the_type_is_rejected(configured):
    """Dr. Osei does not do Procedures."""
    pool, cfg = configured
    with pytest.raises(ProviderCannotPerformType):
        await offer(pool, cfg, appointment_type_id=PROCEDURE, provider_id=2)


async def test_offered_slot_reads_naturally_out_loud(configured):
    pool, cfg = configured
    offers = await offer(pool, cfg, appointment_type_id=FOLLOW_UP, limit=2)
    assert offers[0].spoken_time(cfg) == "Monday the 14th at 9 am"
    assert offers[1].spoken_time(cfg) == "Monday the 14th at 9:15 am"


async def test_seeding_is_idempotent(configured):
    """Seeding runs on every boot, so running it twice must not break anything."""
    pool, cfg = configured
    await seed_from_config(pool, cfg)
    await seed_from_config(pool, cfg)
    assert await pool.fetchval("SELECT count(*) FROM providers") == len(cfg.providers)
    assert await pool.fetchval("SELECT count(*) FROM availability_rules") == len(cfg.domain_rules())


# --- caller preferences --------------------------------------------------------


async def test_morning_preference_returns_only_morning_slots(configured):
    pool, cfg = configured
    offers = await offer(pool, cfg, appointment_type_id=FOLLOW_UP, part_of_day="morning", limit=20)
    assert offers
    assert all(o.slot.start.astimezone(NY).hour < 12 for o in offers)


async def test_afternoon_preference_returns_only_afternoon_slots(configured):
    pool, cfg = configured
    offers = await offer(
        pool, cfg, appointment_type_id=FOLLOW_UP, part_of_day="afternoon", limit=20
    )
    assert offers
    assert all(12 <= o.slot.start.astimezone(NY).hour < 17 for o in offers)


async def test_preference_filtering_is_not_defeated_by_the_offer_limit(configured):
    """The bug this guards: filtering a list already truncated to three would return
    nothing whenever the first three slots fell outside the requested window."""
    pool, cfg = configured
    offers = await offer(
        pool, cfg, appointment_type_id=FOLLOW_UP, part_of_day="afternoon", limit=3
    )
    assert len(offers) == 3, "afternoon slots exist but were filtered away"
    assert offers[0].slot.start.astimezone(NY).hour == 12


async def test_earliest_date_moves_the_search_window(configured):
    pool, cfg = configured
    from datetime import date

    wednesday = date(2026, 9, 16)
    offers = await offer(
        pool, cfg, appointment_type_id=FOLLOW_UP, earliest_date=wednesday, limit=1
    )
    assert offers[0].slot.start.astimezone(NY).date() == wednesday


async def test_an_earliest_date_in_the_past_is_clamped_to_today(configured):
    """A miscalculated date should not hand the caller an empty list."""
    pool, cfg = configured
    from datetime import date

    offers = await offer(
        pool, cfg, appointment_type_id=FOLLOW_UP, earliest_date=date(2020, 1, 1), limit=1
    )
    assert offers
    assert offers[0].slot.start.astimezone(NY).date() == date(2026, 9, 14)


async def test_an_unknown_part_of_day_is_rejected(configured):
    pool, cfg = configured
    with pytest.raises(ValueError, match="unknown part_of_day"):
        await offer(pool, cfg, appointment_type_id=FOLLOW_UP, part_of_day="teatime")
