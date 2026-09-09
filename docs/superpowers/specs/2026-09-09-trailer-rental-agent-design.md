# Trailer Rental Agent — Design

Date: 2026-09-09
Status: approved (design), pending implementation plan

## Overview

A second, independent voice agent for a **trailer rental** business, added alongside the
existing clinic appointment agent (Approach A). It quotes and books trailer rentals over a
real availability backend, answers questions, and transfers to a human. The clinic agent is
**not modified**; the trailer agent is a parallel, well-bounded set of modules that reuses
the proven infrastructure (the audio bridge, signed-token pattern, strict config
validation, and the browser test console).

Both agents run in the same FastAPI process. The trailer agent is wired only when a
`trailer_config.yaml` is present, so existing clinic-only deployments are unaffected.

### Goals
- A genuinely separate agent: own domain data, persona/prompt, tools, voice, and console
  tab.
- Real per-unit availability with **no double-booking**, enforced at the database layer.
- Human-sounding, quick, practical rental-counter persona.
- Testable now in the browser (mic console); real phone routing is a later follow-up.

### Non-goals (YAGNI)
- No inbound phone-number routing yet (the app already answers the clinic number; wiring a
  second number to the trailer agent is a follow-up).
- No payment processing. The quote states the total and the refundable deposit; payment,
  driver's license, and insurance are handled in person at pickup.
- Day-granularity rentals only (no hourly).
- Pricing = daily_rate × days + deposit only (no weekly rates, mileage, or add-ons).
- No rental SMS/reminders yet (could reuse the clinic reminder infra later).

## Locked decisions
- **Collected fields:** first name, last name, phone (required); email (optional) — mirrors
  the clinic booking flow.
- **lookup_rental / cancel_rental** by phone are included now.
- **Voice:** a distinct prebuilt voice from the clinic, env-configurable
  (`TRAILER_GEMINI_VOICE`); default chosen to sound clearly different.
- **Pricing:** `days × daily_rate` (+ deposit stated). Days are inclusive of both the
  pickup and return day: `days = (return_date − pickup_date).days + 1`.

## Data model

New migration `005_trailer_rentals.sql`. Money as `NUMERIC(10,2)`.

- **trailer_types** — `id, name (unique), description, daily_rate, deposit, active`
- **trailer_units** — `id, trailer_type_id → trailer_types, label, active`. N physical units
  per type, seeded from the config `units` count (labels A, B, C…).
- **customers** — `id, name, phone (unique), email, created_at`. Separate from clinic
  `patients` to keep the domains isolated.
- **rentals** —
  `id, trailer_unit_id → trailer_units, customer_id → customers,
   pickup_date DATE, return_date DATE,
   rental_range DATERANGE NOT NULL,
   daily_rate NUMERIC, deposit NUMERIC, total_cost NUMERIC,
   status ENUM('booked','cancelled','completed') DEFAULT 'booked',
   source TEXT DEFAULT 'voice_agent', created_at`
  - `rental_range = daterange(pickup_date, return_date, '[]')` (inclusive both ends), with a
    `CHECK` that it covers `[pickup_date, return_date]`.
  - **Exclusion constraint (the load-bearing safety property):**
    `EXCLUDE USING gist (trailer_unit_id WITH =, rental_range WITH &&) WHERE (status='booked')`
    — a unit cannot be booked for two overlapping date ranges. `btree_gist` is already
    enabled by migration 001.
  - Index on `(trailer_unit_id, rental_range)` where booked; index on `pickup_date`.

Seeding (`db/seed.py` gains a rental path, or a new `db/rental_seed.py`): upsert
`trailer_types` from config; for each, ensure `units` count of `trailer_units` exist.

## Config

New `trailer_config.yaml`, validated at load (`config.py` gains `RentalConfig`, or a new
`rental_config.py` mirroring `config.py`):

```yaml
business:
  name: "Ridgeline Trailer Rentals"
  timezone: "America/New_York"
  phone_display: "(555) 210-4400"
  human_transfer_number: "+1..."
rental:
  min_days: 1
  max_days: 30
  min_lead_days: 0        # earliest pickup relative to today
  booking_horizon_days: 120
trailer_types:
  - id: 1
    name: "6x12 Utility"
    description: "Open utility trailer, 6 by 12 feet, 3,500 lb capacity."
    daily_rate: 45.00
    deposit: 150.00
    units: 3
  - id: 2
    name: "7x14 Enclosed Cargo"
    description: "Enclosed cargo trailer, 7 by 14 feet, ramp door."
    daily_rate: 75.00
    deposit: 250.00
    units: 2
faq:
  - q: "What do I need to bring?"
    a: "A valid driver's license and a vehicle with the right hitch and ball."
  - q: "insurance"
    a: "Your auto insurance typically covers a towed rental; check with your provider."
```

Validation (parallels the clinic): unique type ids and names, positive rates, `units >= 1`,
resolvable timezone, `min_days <= max_days`.

Env: `TRAILER_CONFIG` (default `trailer_config.yaml`), `TRAILER_GEMINI_VOICE`. Signing reuses
`SLOT_TOKEN_SECRET`.

## Domain package `rentals/`

Mirrors `scheduling/`, isolated and independently testable.

- **models.py** — value objects: `TrailerType`, `RentalOption`, `RentalPolicy`.
- **pricing.py** — pure: `quote(daily_rate, deposit, pickup, return) -> Quote(days, total,
  deposit)`.
- **availability.py** — `available_options(pool, cfg, secret, *, type_id|None, pickup,
  return, now)`: validate the date range against policy; find units of each requested type
  with **no overlapping booked rental**; return up to a few numbered `RentalOption`s, each
  bound to one specific free `trailer_unit_id`, priced, with a signed token.
  - Availability SQL: a unit is free when
    `NOT EXISTS (SELECT 1 FROM rentals r WHERE r.trailer_unit_id = u.id AND r.status='booked'
     AND r.rental_range && daterange($pickup,$return,'[]'))`.
- **tokens.py** — `issue_rental_token` / `verify_rental_token` (HMAC, TTL), reusing the
  approach in `scheduling/tokens.py`. Token payload: `trailer_unit_id, trailer_type_id,
  pickup, return, total_cents, deposit_cents`. Amounts as integer cents in the token.
- **booking.py** — transactional, mirrors `scheduling/booking.py`:
  - `book(pool, *, rental_token, secret, customer_name, customer_phone, customer_email,
    now) -> Rental`: per-unit advisory lock → verify token → upsert customer → INSERT rental;
    an `ExclusionViolationError`/`DeadlockDetectedError` becomes `UnitTaken` (the "that one
    just got taken" recovery). Re-derives price from config as defence in depth (token
    amount must match).
  - `cancel(pool, rental_id)`, `find_upcoming_rentals(pool, *, phone, now)` (for
    lookup/cancel authorisation).

## Agent

- **ai/rental_live_config.py** (mirrors `ai/live_config.py`):
  - `build_rental_system_instruction(cfg, now)` — a warm, practical **rental-counter
    persona**: greets, quotes, books, answers questions (hours, hitch/ball, weight limits,
    what to bring, insurance), transfers to a human. Never invents availability or prices —
    only reads tool results. Same "sound human" rules as the clinic (contractions, one–two
    sentences, natural money/dates, small acknowledgements, no menu). Booking flow: work out
    trailer type + pickup/return dates → check availability → quote (days × rate, state the
    deposit) → collect first name, last name, phone (email optional) → read back → book →
    confirm; mention "bring a driver's license and the right hitch at pickup." No AI
    disclosure proactively (matches current clinic setting); truthful if asked.
  - `build_rental_tools(cfg)` — the tool declarations, with `trailer_type` as an enum drawn
    from config so the model can't request a type that doesn't exist.
  - `build_rental_live_config(...)` — reuses the clinic's VAD/affective/model env resolution;
    voice from `TRAILER_GEMINI_VOICE`.

- **agent/rental_tools.py** — `RentalToolRouter` (mirrors `ToolRouter`, same security
  properties: signed tokens, per-call `ToolContext.state`, authorise-before-cancel):
  - `find_available_trailers(trailer_type?, pickup_date, return_date)` → numbered options +
    quote; stores tokens in `ctx.state`.
  - `book_rental(option, first_name, last_name, phone, email?)` → books the chosen option's
    unit; requires first+last+phone; returns dates, type, total, deposit.
  - `lookup_rental(phone?)` → upcoming/active rentals for the caller (authorises them).
  - `cancel_rental(rental_id)` → authorised-only cancel.
  - `answer_faq(question)`, `transfer_to_human(reason)`.

## App wiring

- `main.py` / `app.py`: in the lifespan (after the pool exists), if `TRAILER_CONFIG` resolves
  to an existing file, load + validate the rental config, apply migration/seed, build a
  second Gemini connector (rental prompt/tools/voice) and a `RentalToolRouter`, and store
  them on `AppDeps` (`trailer_connect_gemini`, `trailer_tool_handler`, `trailer_cfg`).
- The audio bridge (`CallSession`) is reused unchanged — it already takes a
  `connect_gemini` + `tool_handler`, so it is agent-agnostic.

## UI — the "Trailer rental" console section

The console sidebar gains a **separate "Trailer rental" group** (only shown when the trailer
agent is configured), with:
- **Test agent** (`/dashboard/trailer/test`) — the mic console, reusing the existing
  test-console renderer parameterised by title + WS path, pointed at a new
  `/dashboard/trailer/testcall` WebSocket that runs a `CallSession` with the trailer
  connector + `RentalToolRouter`.
- **Inventory** (`/dashboard/trailer/inventory`) — trailer types (rate, deposit, unit count)
  and each unit's next booked dates.
- **Rentals** (`/dashboard/trailer/rentals`) — upcoming/active rentals: customer, type,
  pickup→return, total, deposit, status.

Implementation: extract the shared test-console renderer/JS from `web/dashboard.py` into a
reusable helper (parameterised by title, WS path, agent label). Add a small
`web/rental_dashboard.py` router (auth-guarded via the existing session), mounted alongside
the clinic dashboard. Nav gains a grouped "Trailer rental" section in `web/theme.py`.

## Testing

Mirror the clinic's real-Postgres approach (`tests/conftest.py` fixtures):
- `rentals/pricing` — quote math (inclusive days, totals, deposit) — pure unit tests.
- `rentals/availability` — date-range overlap: a unit booked Mon–Wed is unavailable for any
  range touching Mon–Wed, and available from Thu onward (inclusive `[]` ranges mean the
  return day is occupied, so the next rental starts the following day); respects min/max
  days and lead time.
- `rentals/booking` — books; **two concurrent bookings of the last free unit → exactly one
  winner** (the exclusion constraint under load); token duration/amount mismatch refused;
  cancel; lookup.
- `rentals/tokens` — sign/verify, TTL, tamper.
- `agent/rental_tools` — book by option, can't book an un-offered option, cancel
  authorised-only, required first/last/phone, quote surfaced correctly, unknown type
  rejected with the available list.
- `ai/rental_live_config` — key guardrails present in the prompt; tool enums drawn from
  config.
- Dashboard — the three trailer pages render (empty + populated), the testcall WS bridges
  audio + transcript (reuse the `test_testcall` pattern), auth required.
- `scripts/stress_rental_race.py` — concurrency stress proving no double-booked unit
  (mirrors `stress_booking_race.py`).

## Risks / notes
- Rentals span days, so `rental_range` uses `daterange` (`[]` inclusive) rather than the
  clinic's `tstztrange`. The exclusion pattern is otherwise identical.
- Two agents now share the process; keep the trailer modules fully separate so the clinic
  agent's behaviour and tests are untouched.
- Distinct voice + persona so a listener can immediately tell the two agents apart.
