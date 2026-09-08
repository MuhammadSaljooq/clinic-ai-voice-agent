# Clinic AI Voice Agent

Phone agent that answers calls, books/reschedules/cancels appointments, answers
business questions, transfers to a human, and sends SMS reminders.

Telnyx Voice API (bidirectional audio) bridged to the Gemini Live API, with a
Postgres-owned scheduling core and a Google Calendar mirror.

> **⚠️ Prototype posture — synthetic data only.**
> This build uses the Google AI Studio API, which is **not BAA-eligible**. It must
> not take calls from real patients until it is moved to Vertex AI under a Google
> Cloud BAA. See the spec's "HARD BOUNDARY" section.

## Documents

- Design spec: `docs/superpowers/specs/2026-09-08-clinic-voice-agent-design.md`
- Plan 1 (this): `docs/superpowers/plans/2026-09-08-scheduling-core.md`

## Setup

System Python is 3.9 on this machine, so `uv` manages 3.12.

```bash
uv sync --extra dev
```

Postgres runs in Docker (needs the `btree_gist` extension):

```bash
docker run -d --name clinic-pg \
  -e POSTGRES_PASSWORD=clinic -e POSTGRES_USER=clinic -e POSTGRES_DB=clinic \
  -p 55432:5432 postgres:16-alpine
```

Already created it once? Just `docker start clinic-pg`.

Apply the schema:

```bash
docker exec -i clinic-pg psql -U clinic -d clinic < src/clinic_agent/db/migrations/001_init.sql
```

## Tests

```bash
.venv/bin/pytest -q
```

Booking tests run against the real Postgres, on a `clinic_test` database created
automatically. They are not mocked: the exclusion constraint is the core safety
property of this system and it exists only in Postgres.

## Stress the booking race

Proves concurrent callers cannot double-book. This is what caught the
exclusion-constraint deadlock documented in spec §5.1.

```bash
.venv/bin/python scripts/stress_booking_race.py
```

Expect `RESULT: all configs clean` and no growth in
`SELECT deadlocks FROM pg_stat_database WHERE datname='clinic_stress'`.

## Lint

```bash
.venv/bin/ruff check src tests
```

## Running it

Copy `.env.example` to `.env` and fill it in, then:

```bash
.venv/bin/python -m clinic_agent.main
```

Telnyx must be able to reach `PUBLIC_STREAM_URL` from the internet — it cannot
dial your laptop. For local development, tunnel with ngrok and point
`PUBLIC_STREAM_URL` at the tunnel.

**Tool handlers are not wired to the scheduler yet (Plan 3).** The agent will
answer, converse, and disclose that it is an AI, but any booking attempt returns a
structured error and it will offer to transfer instead.

## Status

| Component | State |
|---|---|
| Scheduling core (slots, DST, tokens, booking) | Done |
| Voice bridge (Telnyx ↔ Gemini, barge-in, reconnect) | Done |
| Agent tools wired to the scheduler | Plan 3 |
| Calendar mirror, reminders, inbound SMS | Plan 4 |
| Dashboard | Plan 5 |
