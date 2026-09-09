# Clinic AI Voice Agent

A phone receptionist for a clinic. It answers inbound calls, holds a natural spoken
conversation, and books, reschedules, or cancels appointments — with double-booking made
impossible at the database layer. It also answers business questions, transfers to a
human, handles inbound SMS (opt-out, cancel-by-reply), sends appointment reminders, and
ships a server-rendered operator console.

The caller's audio is bridged in real time to Google's **Gemini Live** (native-audio)
API; telephony (voice call-control, the media WebSocket, and SMS) is **Telnyx**
throughout; the scheduling core is owned by **Postgres**.

> **⚠️ Prototype posture — synthetic data only.**
> The default provider is Google **AI Studio**, which is **not BAA-eligible**. Do not
> point this at a real patient line until it is moved to Vertex AI (`AI_PROVIDER=vertex`)
> under a signed Google Cloud BAA.

---

## Features

- **Conversational booking** over the phone — the agent gathers the caller's **first
  name, last name, and phone number** (email optional), offers real open slots, reads the
  details back, and books.
- **No double-booking, ever** — enforced by a Postgres exclusion constraint, not by
  application logic (proven under load by a stress test).
- **The model can't invent times** — it books by option number against server-side signed
  slot tokens that never enter the prompt.
- **Inbound SMS** — `STOP`/`START`/`HELP` keyword handling and cancel-by-reply (`C`).
- **Appointment reminders** — exactly-once delivery, safe across crashes and redeploys.
- **Operator console** — calls with transcripts, upcoming appointments, the reminder log,
  a two-way SMS inbox, and an in-browser "talk to the agent" mic tester.
- **Optional Google Calendar mirror** of every booking.

## Architecture

```
 Caller ──phone──▶ Telnyx ──webhook──▶ /telnyx/webhook ──▶ answer + open media stream
                     │                                            │
                     └──── bidirectional L16 audio (WSS) ─────────┘
                                     │
                          /telnyx/stream/<secret>
                                     │
                              CallSession  ◀──▶  Gemini Live  (native-audio, tools)
                          (bridge/call_session.py)      │
                                     │            tool calls (book / cancel / …)
                                     ▼                   │
                              ToolRouter (agent/tools.py)▼
                                     │
                          Scheduling core ──▶ Postgres (exclusion constraint, tokens)
```

Everything runs in one FastAPI process. Dependencies are injected via `AppDeps`
(`app.py`) and wired in `main.py`, so the whole app is testable without credentials or a
network. See [`CLAUDE.md`](CLAUDE.md) for the load-bearing design details.

## Requirements

- Python **3.12+** (managed by [`uv`](https://docs.astral.sh/uv/))
- Docker (for local Postgres 16 — needs the `btree_gist` extension)
- A **Telnyx** account (voice number, Call Control application, messaging profile)
- A **Google Gemini** API key (AI Studio for dev; Vertex AI for production)

## Setup

```bash
uv sync --extra dev          # create .venv with dev dependencies

# Local Postgres (db/user/password all "clinic", on port 55432)
docker run -d --name clinic-pg \
  -e POSTGRES_PASSWORD=clinic -e POSTGRES_USER=clinic -e POSTGRES_DB=clinic \
  -p 55432:5432 postgres:16-alpine
# already created once? → docker start clinic-pg
```

Migrations are applied automatically on every boot, so there's no separate schema step.

## Configuration

Copy `.env.example` to `.env` (it is gitignored) and fill it in. Key variables:

| Variable | Purpose |
|---|---|
| `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` | Telnyx API key and the public key that verifies webhooks |
| `TELNYX_NUMBER` | The clinic's number (used for outbound SMS/reminders) |
| `PUBLIC_STREAM_URL` | Public `wss://…/telnyx/stream/<secret>` URL Telnyx streams audio to |
| `STREAM_SECRET` | Secret embedded in the stream URL (the only auth on that socket) |
| `AI_PROVIDER` | `ai_studio` (dev, not BAA-eligible) or `vertex` (production) |
| `GEMINI_API_KEY` | Google AI Studio key (when `AI_PROVIDER=ai_studio`) |
| `DATABASE_URL` | Postgres connection string |
| `SLOT_TOKEN_SECRET` | Signs slot tokens (any long random string) |
| `DASHBOARD_PASSWORD` | Enables the operator console + login page (unset = not mounted) |
| `GOOGLE_CALENDAR_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional calendar mirror |

Business configuration — providers, hours, appointment types, FAQ, and reminder policy —
lives in `config.yaml` and is validated strictly at startup (a bad timezone or a dangling
provider id fails on boot, not mid-call).

## Running it

```bash
.venv/bin/python -m clinic_agent.main      # serves on :8080
```

To take **real calls**, Telnyx must reach your server from the internet — it can't dial
your laptop. For local development, tunnel and point Telnyx at it:

1. Start a tunnel: `cloudflared tunnel --url http://localhost:8080` (or ngrok).
2. Set `PUBLIC_STREAM_URL=wss://<host>/telnyx/stream/<STREAM_SECRET>` in `.env`.
3. In the Telnyx portal:
   - **Voice** — your Call Control application's webhook → `https://<host>/telnyx/webhook`;
     assign your number to it.
   - **Messaging** — your messaging profile's webhook → `https://<host>/telnyx/messaging`.
4. Restart the server and call the number.

## Operator console

Set `DASHBOARD_PASSWORD`, open `/login`, and sign in (a signed-cookie session; 12h). The
console is server-rendered — no build step. Sections under `/dashboard`:

- **Inbox** — two-way SMS threaded per number (inbound texts, auto keyword replies,
  operator replies, and reminders together). Reply from the browser; opt-out is enforced.
  Sending needs `TELNYX_NUMBER`.
- **Calls** — recent calls with full transcripts and outcome.
- **Appointments** — upcoming bookings.
- **Reminders** — every send (or dry-run), newest first.
- **Test agent** — talk to the AI live through your mic, using the same engine that
  answers calls (no phone needed). Half-duplex (mic muted while it speaks) — a real call
  gets carrier-side echo cancellation instead.

The console is read-only except sending SMS and the test call — nothing here can change a
booking, so a leaked session cannot cancel a patient's appointment.

### Voice tuning

The agent's voice and turn-taking are env-configurable without touching code; startup
logs the resolved values (`voice: …`).

| Variable | Effect |
|---|---|
| `GEMINI_VOICE` | Prebuilt voice (e.g. `Aoede`, `Leda`, `Callirrhoe`) |
| `GEMINI_LIVE_MODEL` | Blank = native-audio (most human); `gemini-3.1-flash-live-preview` = faster, plainer |
| `AI_ENABLE_AFFECTIVE_DIALOG` | Liveliness/warmth vs. lower latency |
| `VAD_SILENCE_MS`, `VAD_END_SENSITIVITY` | How fast it decides you're done (snappier vs. less likely to clip you) |
| `AGENT_INTERRUPTIBLE` | `true` = caller can barge in; `false` = agent finishes its sentence |

## Development

```bash
.venv/bin/pytest -q                 # full suite (needs Postgres up)
.venv/bin/pytest tests/test_booking.py -q                 # one file
.venv/bin/pytest -k reminder -q                           # by keyword
.venv/bin/ruff check src tests                            # lint
.venv/bin/python scripts/stress_booking_race.py           # concurrency: proves no double-booking
```

Tests run against **real Postgres** (they create/drop their own `clinic_test` database) —
the exclusion constraint is the core safety property and only exists in Postgres, so
mocking it would test nothing. Migrations are hand-written and idempotent; add the next
numbered file in `src/clinic_agent/db/migrations/`, don't edit applied ones.

## Security notes

- **Webhooks are signature-verified** (`/telnyx/webhook`, `/telnyx/messaging`) before the
  payload is parsed.
- **The media WebSocket** (`/telnyx/stream`) is guarded only by `STREAM_SECRET` in its URL
  — Telnyx does not sign stream frames. Keep the secret out of logs; treat the `start`
  frame's `from` number as advisory, not authenticated.
- **AI Studio is not BAA-eligible** — synthetic data only until `AI_PROVIDER=vertex` + a
  Google Cloud BAA.

## Known gaps

- **Reminders ship off** (`reminders.enabled: false`, `dry_run: true` in `config.yaml`)
  until 10DLC brand/campaign registration is approved. Dry-run records the exact message
  body for review on the dashboard before anything is sent.
- **No real Telnyx call has landed against this code** yet (a trial Telnyx account only
  accepts calls from verified numbers).
- **Calendar mirror is optional** and inert until `GOOGLE_CALENDAR_ID` and
  `GOOGLE_SERVICE_ACCOUNT_JSON` are set.
- **Proactive AI disclosure was removed** at the operator's request — the agent greets
  without stating it's an AI, but still answers truthfully if asked. Some jurisdictions
  (e.g. CA B.O.T. Act) require proactive disclosure; that trade-off is the operator's.

## Design docs

Deeper design rationale lives in `docs/superpowers/specs/` and `docs/superpowers/plans/`.
