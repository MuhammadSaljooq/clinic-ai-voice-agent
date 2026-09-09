# Trailer Rental Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, independent voice agent for a trailer-rental business — real per-unit
availability with no double-booking, a human rental-counter persona, its own tools/config, and
a "Trailer rental" console section — without modifying the clinic agent.

**Architecture:** A parallel `rentals/` domain package mirroring `scheduling/` (pricing,
availability, tokens, transactional booking with a `daterange` exclusion constraint), a
rental prompt/tools (`ai/rental_live_config.py`, `agent/rental_tools.py`), strict config
(`rental_config.py` + `trailer_config.yaml`), a second Gemini connector + tool router wired in
the lifespan, and a `web/rental_dashboard.py` console section reusing the shared audio bridge
and test console. Signing reuses `SLOT_TOKEN_SECRET`.

**Tech Stack:** Python 3.12, FastAPI, asyncpg (raw SQL), Postgres (btree_gist exclusion),
Google Gemini Live, pydantic config, pytest against real Postgres.

Spec: `docs/superpowers/specs/2026-09-09-trailer-rental-agent-design.md`.

Follow existing patterns exactly (compare each new module to its `scheduling/` or clinic
counterpart). TDD, commit per task, on branch `feat/trailer-rental-agent`.

---

### Task 1: Rental config schema + example config

**Files:**
- Create: `src/clinic_agent/rental_config.py` (pydantic `RentalConfig`, mirrors `config.py`)
- Create: `trailer_config.yaml` (example: 2 trailer types, FAQ, business info)
- Test: `tests/test_rental_config.py`

- [ ] Write tests: loads the example config; rejects duplicate type ids/names; rejects
  `units < 1` and non-positive `daily_rate`; rejects `min_days > max_days`; `tz` resolves;
  exposes `trailer_types`, `business`, `rental` (min/max/lead/horizon).
- [ ] Implement `RentalConfig` (models: `BusinessInfo{name,timezone,phone_display,
  human_transfer_number}`, `TrailerTypeConfig{id,name,description,daily_rate:Decimal,
  deposit:Decimal,units:int}`, `RentalRules{min_days,max_days,min_lead_days,
  booking_horizon_days}`, `FaqEntry`), `load_rental_config(path)`, `.tz` property.
- [ ] Run `tests/test_rental_config.py`; commit.

### Task 2: Migration 005 + rental seed

**Files:**
- Create: `src/clinic_agent/db/migrations/005_trailer_rentals.sql`
- Create: `src/clinic_agent/db/rental_seed.py` (`seed_rentals_from_config(pool, cfg)`)
- Test: `tests/test_rental_seed.py`

- [ ] Write migration: `trailer_types`, `trailer_units`, `customers` (phone unique),
  `rentals` with `rental_range DATERANGE`, `CHECK (rental_range = daterange(pickup_date,
  return_date,'[]'))`, status enum, and
  `CONSTRAINT no_double_rental EXCLUDE USING gist (trailer_unit_id WITH =, rental_range WITH &&)
  WHERE (status='booked')`; indexes. Idempotent (`IF NOT EXISTS`, guarded `CREATE TYPE`).
- [ ] Write test: after `apply_all` + `seed_rentals_from_config`, `trailer_types` match config
  and each type has exactly `units` `trailer_units`; re-running seed does not duplicate.
- [ ] Implement seed (upsert types by id; ensure unit count per type, labels A/B/C…).
- [ ] Run tests (real Postgres); commit.

### Task 3: Pricing (pure) + rental tokens

**Files:**
- Create: `src/clinic_agent/rentals/__init__.py`, `src/clinic_agent/rentals/models.py`,
  `src/clinic_agent/rentals/pricing.py`, `src/clinic_agent/rentals/tokens.py`
- Test: `tests/test_rental_pricing.py`, `tests/test_rental_tokens.py`

- [ ] `models.py`: frozen dataclasses `TrailerType`, `RentalSlot{trailer_unit_id,
  trailer_type_id,pickup:date,return_:date}`, `RentalOption{option,type_name,pickup,return_,
  days,total,deposit,token}`.
- [ ] Pricing tests: `quote(daily_rate, deposit, pickup, return_)` → `days = (return_-pickup)
  .days + 1`, `total = days*daily_rate`, deposit passthrough; Mon→Wed = 3 days.
- [ ] Implement `pricing.quote(...) -> Quote(days,total,deposit)` (Decimal money).
- [ ] Tokens tests: `issue_rental_token(slot,total_cents,deposit_cents,secret,now)` +
  `verify_rental_token(token,secret,now)` round-trips; TTL expiry; tamper → `InvalidRentalToken`.
  Model on `scheduling/tokens.py`.
- [ ] Implement `tokens.py`. Run both test files; commit.

### Task 4: Availability

**Files:**
- Create: `src/clinic_agent/rentals/availability.py`
- Test: `tests/test_rental_availability.py`

- [ ] Tests (real Postgres, seeded): a type with 1 unit booked Mon–Wed is unavailable for
  Tue–Thu, available Thu→; respects `min_days`/`max_days`/`min_lead_days`/horizon (returns a
  reason); when a type is given returns ≤1 option bound to a free unit with correct quote +
  signed token; when no type given returns one option per available type; a fully-booked type
  is omitted.
- [ ] Implement `available_options(pool, cfg, secret, *, type_id, pickup, return_, now)`:
  validate range; SQL finds units with `NOT EXISTS (... rentals r ... r.status='booked' AND
  r.rental_range && daterange($pickup,$return,'[]'))`; price via `pricing.quote`; issue token.
- [ ] Run tests; commit.

### Task 5: Booking (transactional) + lookup/cancel

**Files:**
- Create: `src/clinic_agent/rentals/booking.py`
- Test: `tests/test_rental_booking.py`

- [ ] Tests: books a rental (row written, status booked, total/deposit correct); **two
  concurrent bookings of the last free unit → exactly one `Booked`, one `UnitTaken`** (via
  `asyncio.gather`); token/type/amount mismatch → `ValueError`; `cancel` flips status and frees
  the dates; `find_upcoming_rentals(phone)` returns booked future rentals only.
- [ ] Implement mirroring `scheduling/booking.py`: `_serialize_unit_writes` (advisory lock on
  `trailer_unit_id`), `_upsert_customer(name,phone,email)`, `book(pool,*,rental_token,secret,
  customer_name,customer_phone,customer_email,now) -> Booked`, catching
  `ExclusionViolationError`/`DeadlockDetectedError` as `UnitTaken`; `cancel(pool,rental_id)`;
  `find_upcoming_rentals(pool,*,phone,now)`.
- [ ] Run tests; add `scripts/stress_rental_race.py` (mirror booking race) and run once; commit.

### Task 6: Rental prompt + tool declarations

**Files:**
- Create: `src/clinic_agent/ai/rental_live_config.py`
- Test: `tests/test_rental_live_config.py`

- [ ] Tests: prompt contains the persona/guardrails (mentions rentals, "never invent"
  availability/prices, transfer, bring-license-at-pickup); `build_rental_tools(cfg)` declares
  `find_available_trailers`, `book_rental`, `lookup_rental`, `cancel_rental`, `answer_faq`,
  `transfer_to_human` with `trailer_type` as an enum from config; `build_rental_live_config`
  applies voice + reuses VAD/affective env.
- [ ] Implement mirroring `ai/live_config.py`: `build_rental_system_instruction(cfg,now)`
  (warm rental-counter persona; booking flow: type+dates → availability → quote → first/last/
  phone (+email) → read back → book → confirm; no proactive AI disclosure; sound-human rules),
  `build_rental_tools(cfg)`, `build_rental_live_config(cfg,*,now,voice,vad,affective)`.
- [ ] Run tests; commit.

### Task 7: RentalToolRouter

**Files:**
- Create: `src/clinic_agent/agent/rental_tools.py`
- Test: `tests/test_rental_tools.py`

- [ ] Tests (real Postgres, seeded): `find_available_trailers` returns numbered options +
  quote and stores tokens; `book_rental` by option writes the rental (first/last/phone
  required, email stored); booking an un-offered option is refused; `cancel_rental`
  authorised-only (must be looked up on this call); unknown `trailer_type` returns the
  available list; `answer_faq`/`transfer_to_human`.
- [ ] Implement `RentalToolRouter` mirroring `agent/tools.py` (per-call `ToolContext.state`
  offers + authorised ids; signed tokens; `_as_int`).
- [ ] Run tests; commit.

### Task 8: App wiring (second agent)

**Files:**
- Modify: `src/clinic_agent/app.py` (`AppDeps`: add `trailer_cfg`, `trailer_connect_gemini`,
  `trailer_tool_handler`)
- Modify: `src/clinic_agent/main.py` (lifespan: if `TRAILER_CONFIG` file exists, load+validate,
  seed, build rental connector + `RentalToolRouter`, store on deps; log)
- Modify: `.env.example` (`TRAILER_CONFIG`, `TRAILER_GEMINI_VOICE`)
- Test: `tests/test_app.py` (health still fine; deps optional-field defaults None)

- [ ] Add optional deps fields (default None) — existing tests unaffected. Build a
  `build_gemini_connector` variant for the rental config (reuse `ai/live_session.py`,
  parameterising cfg/prompt/tools/voice — add `build_rental_connector` or generalise).
- [ ] Wire in the lifespan behind a file-exists check; migrations already apply 005 on boot;
  seed rentals. Run `tests/test_app.py`; commit.

### Task 9: Console "Trailer rental" section

**Files:**
- Modify: `src/clinic_agent/web/theme.py` (nav gains a grouped "Trailer rental" section; shared
  test-console renderer extracted so both agents reuse it)
- Create: `src/clinic_agent/web/rental_dashboard.py` (`build_rental_dashboard(cfg, pool_getter,
  password, *, connect_gemini, tool_handler_getter)`: `/dashboard/trailer/test` page,
  `/dashboard/trailer/testcall` WS, `/dashboard/trailer/inventory`, `/dashboard/trailer/rentals`)
- Modify: `src/clinic_agent/web/dashboard.py` (extract `render_test_console(cfg,*,title,ws_path,
  active,inbox_count)` used by both; keep clinic behaviour identical)
- Modify: `src/clinic_agent/app.py` (mount the rental router when the trailer agent is wired)
- Test: `tests/test_rental_dashboard.py`

- [ ] Extract the shared test-console renderer from `dashboard.py`; re-run `test_testcall.py`
  and `test_dashboard.py` to confirm the clinic console is unchanged.
- [ ] Implement the rental router + pages; test: pages render (empty + populated), testcall WS
  bridges audio + transcript (reuse `test_testcall` pattern with the rental connector), auth
  required, nav shows the Trailer-rental group only when wired.
- [ ] Run tests; commit.

### Task 10: Full verification

- [ ] `.venv/bin/ruff check src tests` clean.
- [ ] `.venv/bin/pytest -q` full suite green.
- [ ] Boot the app with `trailer_config.yaml`; browser-verify the Trailer-rental tab renders
  and (if a valid `GEMINI_API_KEY`) the test agent connects. Screenshot.
- [ ] Update `README.md` (mention the second agent + `TRAILER_CONFIG`) and `CLAUDE.md`
  (trailer domain mirrors scheduling). Commit.

## Self-review notes
- Spec coverage: config (T1), DB+seed (T2), pricing/tokens (T3), availability (T4), booking+
  race (T5), prompt/tools (T6), router (T7), wiring (T8), UI (T9), verify+docs (T10) — all
  spec sections covered.
- Money as `Decimal` in DB/pricing; integer cents inside tokens (avoid float drift).
- Inclusive `daterange('[]')` used consistently in migration, availability SQL, and booking.
- Clinic agent files are only touched to *extract a shared renderer* (T9) and add *optional*
  deps (T8); its behaviour/tests must stay identical.
