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

The agent can now answer, disclose that it is an AI, find slots, book, reschedule,
cancel, answer questions from `config.yaml`, and transfer to a human.

All five phases are built. Verified against the real Gemini API by a simulated
call that booked an appointment end to end; **not yet verified against Telnyx**,
which needs a call to land (see Known gaps).

## Dashboard

Set `DASHBOARD_PASSWORD` in `.env` and visit `/dashboard`. Any username, that
password. Three read-only pages: recent calls with transcripts, upcoming
appointments, and the reminder log. Leave the variable unset and the dashboard is
not mounted at all.

## Known gaps

- **Reminders are off.** `reminders.enabled: false` in `config.yaml` until 10DLC
  brand and campaign registration is approved. While off, the agent is explicitly
  told not to promise a reminder. `dry_run: true` records the exact message body
  so the wording can be reviewed on the dashboard before anyone is texted.
- **Calendar mirror is optional** and inert until `GOOGLE_CALENDAR_ID` and
  `GOOGLE_SERVICE_ACCOUNT_JSON` are set. Share the clinic calendar with the service
  account email; no OAuth verification is required.
- **Telnyx has never delivered a real call to this code.** A trial Telnyx account
  only accepts calls from verified numbers, which is what produced a busy signal
  on the first attempt.
- **Reply latency measured 7-8s** on the simulated call, against a target of under
  800ms. Disabling model thinking did not fix it; the remaining suspect is
  end-of-speech VAD sensitivity.
- **AI Studio is not BAA-eligible.** Synthetic data only until `AI_PROVIDER=vertex`
  and a Google Cloud BAA are in place.

## Status

| Component | State |
|---|---|
| Scheduling core (slots, DST, tokens, booking) | Done |
| Voice bridge (Telnyx ↔ Gemini, barge-in, reconnect) | Done |
| Agent tools wired to the scheduler | Done |
| Calendar mirror, reminders, inbound SMS | Done |
| Operator dashboard | Done |
