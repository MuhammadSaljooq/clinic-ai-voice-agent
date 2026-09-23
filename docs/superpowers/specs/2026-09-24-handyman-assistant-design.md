# Handyman Virtual Assistant (Task Titan) — Design

**Goal:** A third, independent voice agent — a phone assistant for Task Titan Handyman —
that answers service questions, takes appointment (visit/estimate) requests, captures
callback leads, and takes personal messages for the owner. It gets its own login-picker
tab and a four-page operator console.

**Status:** approved 2026-09-24.

## Context

Third parallel agent alongside the clinic and trailer agents. It mirrors the trailer
agent's structure module-for-module, so the clinic and trailer code are untouched. Wired
only when `handyman_config.yaml` exists (env `HANDYMAN_CONFIG`), exactly like the trailer
agent is gated on `trailer_config.yaml`.

Business facts (from tasktitanhandyman.com): owner **Michael**, phone 978-790-7000,
email michael@tasktitanhandyman.com, 20+ years' experience, licensed & insured, free
estimates (no published rates). **Service area: Knoxville, TN** (user's choice; the
website's 978 area code / HIC #218504 are Massachusetts-specific, so the phone/license in
config are placeholders to correct, and the prompt says "licensed and insured" rather than
quoting a state license number).

## Booking model — requests only

Handymen quote after seeing the job, so there is **no live calendar and no slot engine**.
The agent records an appointment *request* (name, phone, job description, address,
preferred day/time) for Michael to confirm. The agent never says "you're booked" — it says
"I've noted that down and Michael will confirm." This diverges from the clinic/trailer
slot-token model on purpose: there is nothing to double-book and no price to invent (the
agent only ever offers a *free estimate*).

## Components (new files, mirroring the trailer agent)

| Trailer | Handyman |
|---|---|
| `rental_config.py` / `trailer_config.yaml` | `handyman_config.py` / `handyman_config.yaml` |
| `ai/rental_live_config.py` | `ai/handyman_live_config.py` |
| `agent/rental_tools.py` (`RentalToolRouter`) | `agent/handyman_tools.py` (`HandymanToolRouter`) |
| `web/rental_dashboard.py` | `web/handyman_dashboard.py` |
| `db/migrations/005_trailer_rentals.sql` | `db/migrations/008_handyman.sql` |
| `build_rental_connector` | `build_handyman_connector` |
| — (test console not persisted) | `db/handyman_calls.py` (persists console + phone calls) |

### Config (`handyman_config.py`, pydantic, strict-at-load)
`BusinessInfo` (name, timezone `America/New_York`, human_transfer_number, phone_display,
service_area, owner_name, email), a list of `ServiceCategory` (name + example tasks), and
`faq` entries. No inventory, so no seed step — only the migration runs.

### Tools (`HandymanToolRouter`)
1. `answer_question` — FAQ/service answer via `match_faq` over config.
2. `request_appointment` — insert into `handyman_appointment_requests` (name, phone,
   job_type, description, address, preferred_time). Reads back the phone; never claims a
   confirmed booking.
3. `capture_lead` — insert into `handyman_leads` (name, phone, reason).
4. `take_message` — insert into `handyman_messages` (caller_name, phone, message) — for a
   personal (non-business) call for Michael.
5. `transfer_to_human` — to the owner's number (parity with the other agents).

All inserts are inline in the router (the clinic `_request_callback` pattern). Phone falls
back to `ctx.caller_number`; state keys remember the name/phone across a call.

### Prompt (`build_handyman_system_instruction`)
Warm, competent office assistant for Michael. Sections: current date/time; warm greeting;
what it does; **absolute limits** (never quote prices — offer a free estimate; never invent
services/hours; anything it can't do → take a message or transfer); how to sound human;
the service list from config; the FAQ; the three intake flows (appointment request, lead,
personal message) with read-backs; ending the call. Native-audio/fast-model aware
(`native_audio` strips affective dialog, thinking, and NON_BLOCKING behaviours).

### Data model (`008_handyman.sql`, idempotent)
- `handyman_appointment_requests` (id, name, phone NOT NULL, job_type, description,
  address, preferred_time, status enum `requested|confirmed|declined|done`, timestamps).
- `handyman_leads` (id, name, phone NOT NULL, reason, status enum `new|contacted|closed`,
  timestamps).
- `handyman_messages` (id, caller_name, phone, message NOT NULL, status enum `new|read`,
  created_at).
- `handyman_calls` (id, from_number, source enum `phone|console`, started_at, ended_at,
  outcome, transcript jsonb). `telnyx_call_control_id` UNIQUE-nullable for phone-call
  upserts; console calls always insert.

### Console (`web/handyman_dashboard.py`, prefix `/dashboard/handyman`)
Own shell (mirrors rental `_shell`), four pages:
1. **Voice assistant** (`/voice`, landing) — the shared mic test console. On session end,
   the WebSocket persists the transcript to `handyman_calls` (source `console`) so the
   Transcriptions page has data before a phone line exists.
2. **Appointments** (`/appointments`) — request rows; POST `/{id}/status` cycles status.
3. **Transcriptions** (`/transcriptions`) — `handyman_calls` rows with transcripts
   (reusing `render_transcript`).
4. **Leads & Messages** (`/leads`) — two sections; POST `/leads/{id}/contacted` and
   `/messages/{id}/read` mark handled.

Read-only except the status/mark actions (no booking to mutate).

### Login picker (`web/auth.py` refactor)
Generalise the two-workspace picker to an ordered `workspaces` list of
`(key, label, landing, prefix)`. `app.py` builds it: clinic always; trailer if configured;
handyman if configured. Landing uses longest-prefix matching so a `?next` is honoured only
within the chosen workspace, else that workspace's home. Picker hidden when only one
workspace exists.

### Wiring
- `main.py`: load `handyman_config.yaml` + `build_handyman_connector` at build time; build
  `HandymanToolRouter` in the lifespan (needs the pool); no seed.
- `app.py` `AppDeps`: `handyman_cfg`, `handyman_connect_gemini`, `handyman_tool_handler`.
  Mount the handyman dashboard when `handyman_cfg` is present; add it to the workspaces
  list.
- Voice: `HANDYMAN_GEMINI_VOICE` (default `Charon`). `.env.example` gains
  `HANDYMAN_CONFIG` and `HANDYMAN_GEMINI_VOICE`.

## Testing (mirror the trailer suite)
- `test_handyman_config.py` — loads the shipped yaml; strict validation.
- `test_handyman_tools.py` — each intake tool inserts the right row; phone fallback;
  missing-phone recovery; unknown tool.
- `test_handyman_live_config.py` — all tools declared; prompt names the business/service
  area, forbids price quotes; native-audio stripping.
- `test_handyman_dashboard.py` — all four pages render (empty + populated); status/mark
  actions; read-only guards; console call recording surfaces on Transcriptions.
- `test_auth.py` — three-workspace picker; each tab lands on its console; longest-prefix
  next handling.

## Out of scope
No live phone number (transcriptions populate from the console until one is wired). No
SMS/reminders for the handyman agent. No pricing/quoting. No calendar mirror.
