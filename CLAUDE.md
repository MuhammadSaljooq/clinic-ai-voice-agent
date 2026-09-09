# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A clinic phone receptionist: it answers inbound calls, bridges the caller's audio to
Google **Gemini Live** (native-audio) so the agent can converse, and books/reschedules/
cancels appointments against Postgres. It also handles inbound **SMS** (opt-out, cancel),
sends appointment reminders, and serves a server-rendered **operator console** (calls,
appointments, reminders, and a two-way SMS inbox + a browser "test the agent" mic
console). Telephony is **Telnyx** throughout (voice call-control, media WebSocket, SMS) —
not Twilio.

Python 3.12, FastAPI, asyncpg (raw SQL, **no ORM**), managed with **uv**.

## Commands

```bash
# Postgres for tests + local dev (port 55432, db/user/pass all "clinic")
docker run -d --name clinic-pg -e POSTGRES_PASSWORD=clinic -e POSTGRES_USER=clinic \
  -e POSTGRES_DB=clinic -p 55432:5432 postgres:16-alpine

uv sync --extra dev                 # create .venv with dev deps

.venv/bin/pytest -q                 # full suite (needs Postgres up; ~45s)
.venv/bin/pytest tests/test_booking.py -q          # one file
.venv/bin/pytest tests/test_booking.py::test_booking_writes_an_appointment -q   # one test
.venv/bin/pytest -k reminder -q     # by keyword

.venv/bin/ruff check src/ tests/    # lint (must be clean; CI-equivalent gate)

.venv/bin/python -m clinic_agent.main               # run the server (reads .env, port 8080)
.venv/bin/python scripts/stress_booking_race.py     # concurrency stress: proves no double-booking
```

Tests spin up/drop their own `clinic_test` database from `ADMIN_DATABASE_URL`
(`postgresql://clinic:clinic@localhost:55432/postgres`); the app itself uses `DATABASE_URL`.

## Load-bearing design (read these together, they're the point)

- **Signed slot tokens = the model cannot invent bookable times.** `find_slots`
  (`agent/tools.py`) records HMAC-signed slot tokens in **per-call `ToolContext.state`**
  and returns plain option *numbers*. The model books by number; tokens never enter the
  prompt. `scheduling/tokens.py` + `booking.py` verify them. Reschedule/cancel are
  authorised only against appointment ids that `lookup_appointment` returned *on this
  call* — a guessed id can't touch a stranger's appointment.
- **Double-booking is impossible at the DB layer.** `db/migrations/001_init.sql` has a
  GiST `EXCLUDE` constraint on `(provider_id, blocked_range)` where `status='booked'`.
  `blocked_range` is the appointment plus denormalised buffers. `booking.py` takes a
  per-provider advisory lock first so concurrent overlapping inserts fail cleanly as
  `SlotTaken` instead of deadlocking. The stress script exists to defend this.
- **Reminders are exactly-once via claim-then-send.** `reminders/worker.py`: a UNIQUE
  `reminders.appointment_id` makes the claim atomic; a durable `sending` marker is written
  *before* the Telnyx call so crash recovery can retry a never-sent claim without ever
  double-texting. `reminders.enabled=false` + `dry_run=true` (in `config.yaml`) until
  10DLC registration; dry-run records the exact body for review.
- **The voice bridge is one class, three tasks.** `bridge/call_session.py` `CallSession`
  runs reader (Telnyx frames → inbound queue), writer (outbound queue → paced 20ms
  frames), and a gemini loop that owns the Live session and reconnects across resets. The
  inbound queue survives a reconnect; the session doesn't. Barge-in clears Telnyx's buffer
  (`encode_clear`). The same class backs the browser test console (`web/dashboard.py`
  `_BrowserSocket`), which speaks the identical Telnyx frame protocol.
- **Audio format is exact and easy to get wrong.** `audio/codec.py`: Telnyx L16 is
  **big-endian** 16kHz; Gemini wants little-endian; outbound also resamples 24k→16k.
  Wrong byte order = loud static, not silence. The browser console encodes/decodes
  big-endian 16k to match.
- **Dependencies are injected, not read from env inside handlers.** `app.py` `AppDeps`
  holds everything (`cfg`, `telnyx`, `connect_gemini`, `pool`, secrets…); `main.py`
  `build_app` wires real deps and a lifespan that creates the pool, applies migrations,
  seeds from `config.yaml`, and starts the reminder loop. This is why the whole app is
  testable without credentials or network.

## Config, migrations, secrets

- Business config lives in `config.yaml` (providers, hours, appointment types, FAQ,
  reminder policy) and is **validated strictly at load** (`config.py`, pydantic) — a bad
  timezone or dangling provider id fails on boot, not mid-call.
- Migrations (`db/migrations/NNN_*.sql`) are hand-written, idempotent, and applied in
  order on every boot (`db/migrate.py`). Add the next number; don't edit applied ones.
- `.env` is the source of truth: `main.py` calls `load_dotenv(..., override=True)`, so a
  stale shell env var will **not** shadow `.env` (this was a real footgun). See
  `.env.example` for all vars.
- Voice behaviour is env-tunable without code changes: `GEMINI_VOICE`,
  `GEMINI_LIVE_MODEL` (blank = native-audio; the half-cascade model is faster but
  plainer), `AI_ENABLE_AFFECTIVE_DIALOG` (liveliness vs latency), `VAD_SILENCE_MS` /
  `VAD_END_SENSITIVITY` (reply latency vs cutting callers off), `AGENT_INTERRUPTIBLE`
  (barge-in on/off). `main.py` logs the resolved values as `voice: ...` at startup.
- `STREAM_SECRET` guards the media WebSocket (`/telnyx/stream/<secret>` or `?token=`) —
  Telnyx doesn't sign stream frames, so this is the only auth on that socket.
  `AI_PROVIDER=ai_studio` is **not BAA-eligible** (synthetic data only); real patient
  traffic needs `AI_PROVIDER=vertex`.

## Operator console

Mounted only when `DASHBOARD_PASSWORD` is set. Auth is a **signed-cookie session** with a
login page (`web/auth.py`), not HTTP Basic. Pages are server-rendered from
`web/dashboard.py` using the design system in `web/theme.py` (no build step, no JS
framework). The console is read-only except sending SMS from the inbox and the browser
test call — it can't touch a booking, so a leaked session can't cancel an appointment.

## Testing conventions

- Tests run against **real Postgres** (the exclusion constraint only exists there);
  `tests/conftest.py` provides the `pool`/`clinic` fixtures. `tests/fakes.py` provides
  Gemini/Telnyx doubles built from real `google.genai.types`.
- For async HTTP surfaces use **httpx `ASGITransport`**, not Starlette `TestClient` —
  asyncpg connections are bound to the loop that created them, and `TestClient` runs on a
  separate loop (this bites; see `test_dashboard.py`'s note). `TestClient` is fine where
  there's no DB pool (WebSocket tests in `test_app.py`).
- Ruff is enforced. Note: broad `except Exception:` must **log** in the handler (the
  repo's convention, per `db/calls.py`) or it trips the linter. `python-multipart` is
  **not** installed — form posts are parsed manually (`web/auth.py:read_form`), don't
  reach for FastAPI `Form()`.

## Known gaps

See `README.md`. In short: reminders are dry-run/off until 10DLC; the calendar mirror is
optional/inert without Google creds; no real Telnyx call has been placed from this code;
AI Studio isn't BAA-eligible. The browser test console is half-duplex (mic muted while the
agent speaks) because browser echo-cancellation misses Web Audio output — a real phone
call gets carrier-side echo cancellation instead.
