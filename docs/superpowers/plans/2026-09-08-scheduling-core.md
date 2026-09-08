# Scheduling Core Implementation Plan (Plan 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully tested appointment scheduling core that turns provider availability into bookable slots and books them without double-booking.

**Architecture:** `scheduling/slots.py` is a **pure function** — no DB, no network, no clock reads — so the densest logic in the system (buffers, closures, DST, lead time) is exhaustively unit-testable with zero mocks. `scheduling/booking.py` wraps it in a Postgres transaction whose GiST exclusion constraint makes double-booking structurally impossible. Working time is computed by converting local interval **endpoints** to UTC and stepping in UTC, which makes DST correct by construction rather than by special-casing.

**Tech Stack:** Python 3.12 (uv-managed), asyncpg (no ORM — direct control over `FOR UPDATE` and the exclusion constraint), Postgres 16 in Docker, pytest + time-machine.

**Why this plan first:** It is step 1 of the spec's build order. It has the highest bug density and the cheapest tests, and it produces working software (bookable appointments) with no telephony involved.

---

## Verified environment

| Thing | Value |
|---|---|
| System Python | 3.9.6 — **too old**, so uv manages 3.12 |
| Venv Python | 3.12.12 at `.venv/bin/python` |
| Postgres | 16.15 in Docker, `btree_gist` available |
| Connect | `postgresql://clinic:clinic@localhost:55432/clinic` |
| Start DB | `docker start clinic-pg` |

---

## File structure

| File | Responsibility |
|---|---|
| `src/clinic_agent/scheduling/models.py` | Frozen dataclasses. No behaviour. |
| `src/clinic_agent/scheduling/slots.py` | **Pure.** Availability → bookable slots. |
| `src/clinic_agent/scheduling/tokens.py` | Sign/verify opaque `slot_token`. |
| `src/clinic_agent/db/migrations/001_init.sql` | Schema incl. exclusion constraint. |
| `src/clinic_agent/db/pool.py` | asyncpg pool. |
| `src/clinic_agent/scheduling/booking.py` | Transactional book/reschedule/cancel. |
| `src/clinic_agent/config.py` | Load + validate `config.yaml`. |
| `tests/test_slots.py` | Table-driven slot tests. |
| `tests/test_slots_dst.py` | DST both directions. |
| `tests/test_tokens.py` | Token tamper resistance. |
| `tests/test_booking.py` | Real Postgres; concurrency. |
| `tests/test_config.py` | Config validation. |

---

## Domain rules (locked, so tests are unambiguous)

1. The appointment **body** `[start, start+duration]` must fit entirely inside a working interval.
2. **Buffers do not need to fit inside working hours** — they exist for provider turnaround. So a 10-min `buffer_before` does not block the first slot of the day.
3. Two appointments conflict when their **buffered** regions overlap:
   `[start - buffer_before, end + buffer_after]`.
4. Candidate starts are aligned to the **working-interval start**, stepping by `granularity_min`. A 9:00–17:00 day at 15 min yields 9:00, 9:15, …
5. Slots earlier than `now + min_lead_time_min` or later than `now + booking_horizon_days` are excluded.
6. All storage and all arithmetic is **UTC**. Local time exists only at the config boundary and in speech.
7. An `AvailabilityException` with `is_closed` removes the day. With `start`/`end` set it **replaces** that day's rules. Both unset + not closed is invalid config.

---

### Task 1: Domain models

**Files:** Create `src/clinic_agent/scheduling/models.py`, `src/clinic_agent/scheduling/__init__.py`

- [ ] **Step 1: Write the models**

```python
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
    """Recurring weekly availability. weekday: 0=Monday .. 6=Sunday (local time)."""
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
    """Dated override. is_closed removes the day; start/end replace the day's rules."""
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
    """An existing appointment occupying a provider's time. UTC, tz-aware."""
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
```

- [ ] **Step 2: Verify it imports and validates**

Run: `.venv/bin/python -c "from clinic_agent.scheduling.models import AppointmentType as T; T(1,'x',0)"`
Expected: `ValueError: duration_min must be positive, got 0`

- [ ] **Step 3: Commit**

```bash
git add src/clinic_agent/scheduling/ && git commit -m "feat: scheduling domain models"
```

---

### Task 2: Pure slot generation

**Files:** Create `src/clinic_agent/scheduling/slots.py`, `tests/test_slots.py`

- [ ] **Step 1: Write the failing tests** (see `tests/test_slots.py` — covers: basic grid, duration fit, buffer conflict, first-slot-of-day with buffer_before, closure, replaced hours, lead time, horizon, multi-provider, limit)

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/pytest tests/test_slots.py -q`
Expected: collection error — `ModuleNotFoundError: clinic_agent.scheduling.slots`

- [ ] **Step 3: Implement `find_slots`**

Algorithm per provider per date: resolve working intervals (exception overrides rules) → convert **local endpoints to UTC** → step by granularity from interval start → keep candidates whose body fits the interval, whose buffered region misses every buffered busy region, and which fall inside `[now+lead, now+horizon]`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/pytest tests/test_slots.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

---

### Task 3: DST correctness

**Files:** Create `tests/test_slots_dst.py`

Endpoint-conversion means DST is handled by construction; these tests prove it and guard the regression.

- [ ] **Step 1:** Spring forward (2026-03-08, America/New_York): a 09:00–17:00 local day spans **7 real hours**, so a 60-min type yields 7 slots, not 8.
- [ ] **Step 2:** Fall back (2026-11-01): the same local day spans **9 real hours** → 9 slots.
- [ ] **Step 3:** Slot local wall-clock times still read 09:00, 10:00, … on both days.
- [ ] **Step 4:** Run `.venv/bin/pytest tests/test_slots_dst.py -q`; commit.

---

### Task 4: Slot tokens

**Files:** Create `src/clinic_agent/scheduling/tokens.py`, `tests/test_tokens.py`

The model must never construct a booking time from free text. `find_slots` issues an HMAC-signed token encoding provider/type/start/end; `book` accepts only a valid, unexpired token.

- [ ] **Step 1:** Tests: round-trip; tampered payload rejected; expired rejected; wrong secret rejected.
- [ ] **Step 2:** Run, confirm fail.
- [ ] **Step 3:** Implement with `hmac.compare_digest` (never `==`) and base64url.
- [ ] **Step 4:** Run, confirm pass. Commit.

---

### Task 5: Schema with the exclusion constraint

**Files:** Create `src/clinic_agent/db/migrations/001_init.sql`, `src/clinic_agent/db/pool.py`

- [ ] **Step 1:** Write `001_init.sql` per spec §5, including:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
...
  EXCLUDE USING gist (
    provider_id WITH =,
    tstzrange(starts_at, ends_at) WITH &&
  ) WHERE (status = 'booked')
```

- [ ] **Step 2:** Apply and verify the constraint actually rejects an overlap:

```bash
docker exec -i clinic-pg psql -U clinic -d clinic < src/clinic_agent/db/migrations/001_init.sql
```
Expected: two overlapping inserts → second fails with `conflicting key value violates exclusion constraint`

- [ ] **Step 3:** Commit.

---

### Task 6: Transactional booking

**Files:** Create `src/clinic_agent/scheduling/booking.py`, `tests/test_booking.py`

- [ ] **Step 1:** Tests against real Postgres: book succeeds; double-book of one slot → exactly one winner; **two concurrent bookings via `asyncio.gather` → exactly one winner**; cancel frees the slot; reschedule moves it atomically; invalid token rejected.
- [ ] **Step 2:** Run, confirm fail.
- [ ] **Step 3:** Implement. `book` verifies token → opens transaction → re-checks availability → inserts, catching `ExclusionViolationError` and returning a typed `SlotTaken` result rather than raising.
- [ ] **Step 4:** Run, confirm pass. Commit.

---

### Task 7: Config loading

**Files:** Create `src/clinic_agent/config.py`, `config.yaml`, `tests/test_config.py`

- [ ] **Step 1:** Tests: valid config loads; unknown timezone rejected; appointment type restricted to a non-existent provider rejected; overlapping rules for one provider rejected.
- [ ] **Step 2:** Run, confirm fail.
- [ ] **Step 3:** Implement with pydantic; validate timezone via `ZoneInfo` lookup.
- [ ] **Step 4:** Run, confirm pass. Commit.

---

## Definition of done

- [ ] `.venv/bin/pytest -q` fully green
- [ ] DST tested in both directions
- [ ] Concurrent double-booking proven impossible against real Postgres
- [ ] No placeholder code, no `TODO`

## Next plans

2. Voice bridge (Telnyx ↔ Gemini, barge-in, session resumption)
3. Agent tools + persona wiring
4. Calendar mirror + reminder worker + inbound SMS
5. Dashboard
