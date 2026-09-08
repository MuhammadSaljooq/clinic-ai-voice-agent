# Clinic AI Voice Agent — Design Spec

**Date:** 2026-09-08
**Phase:** 1 of 4 (voice engine + scheduling core)
**Posture:** Prototype, synthetic data only
**Stack:** Python 3.12+, FastAPI, Postgres, Telnyx Voice API, Gemini Live API

---

## ⚠️ HARD BOUNDARY — READ BEFORE GOING LIVE

This build uses the **Google AI Studio / Gemini Developer API**, which is
**not BAA-eligible** and is documented by Google as a prototyping environment,
not for production PHI workloads.

**This system must not receive calls from real patients in its current
configuration.** Patient name + phone + appointment time + reason for visit is
PHI under HIPAA. So is an appointment reminder SMS, because it reveals that a
person is a patient of a specific provider.

Before any real patient calls:

1. Move the AI provider to **Vertex AI** (`vertexai=True` + project/location).
   Same models, same Live API. See `ai/provider.py` — this is a config flip.
2. Execute a **Google Cloud BAA**.
3. Execute a **Telnyx BAA** (Telnyx signs these and offers HIPAA-eligible services).
4. Complete **10DLC** brand + campaign registration for SMS.
5. Add the deferred PHI safeguards listed in "Deferred to Go-Live" below.

Development and testing use **synthetic patient data only**.

---

## 1. Goals

Build a phone agent on an existing Telnyx number that:

- Answers inbound calls with natural, low-latency, emotionally responsive speech
- Books, reschedules, and cancels appointments against a real availability model
- Answers business questions (hours, location, parking, what to bring)
- Transfers to a human when it should not or cannot handle the call
- Sends an SMS reminder 24 hours before each appointment

### Non-goals (explicitly out of scope for Phase 1)

- Multi-tenant onboarding, provisioning, billing (Phases 2–3)
- EHR / practice-management integration
- Any form of clinical advice, triage, or symptom assessment
- Outbound sales or campaign calling
- Payment collection

---

## 2. Verified platform constraints

These were confirmed against vendor documentation and drive the design.

| Constraint | Detail | Consequence |
|---|---|---|
| Telnyx bidirectional audio | `stream_bidirectional_mode: "rtp"`, codec **L16 @ 16 kHz** | Matches Gemini input exactly |
| Gemini Live audio I/O | In: **16 kHz** PCM16 LE · Out: **24 kHz** PCM16 LE | One downsample, outbound only |
| Affective dialog | `enable_affective_dialog: true`, **v1beta**, native-audio model only | Pins the model — see 2.1 |
| WebSocket lifetime | Connection resets at **~10 min**; audio session caps at **15 min** uncompressed | Session resumption + context compression are **mandatory**, not optional |
| Interruption | Gemini emits `interrupted`; Telnyx accepts `{"event":"clear"}` | Barge-in is wired between the two |
| AI disclosure | CA AB 2905: within **15 s**. TX SB 140: within **30 s** | Disclosure goes in the greeting's first sentence |
| Healthcare AI | CA AB 489 bars AI from implying healthcare credentials | Hard guardrail: no clinical advice, no credential claims |
| US A2P SMS | 10DLC brand + campaign required | Reminders are gated on registration |

### 2.1 Model selection (and why it is pinned)

**Chosen: `gemini-2.5-flash-native-audio-preview-12-2025`** via v1beta.

| Capability | `2.5-flash-native-audio-preview-12-2025` | `3.1-flash-live-preview` |
|---|---|---|
| Affective dialog (reads caller tone) | Yes | **No** — docs: "remove any configuration for these features" |
| Proactive audio | Yes | **No** |
| Async (`NON_BLOCKING`) function calling | Yes | **No** — sequential only |
| Native audio out, function calling | Yes | Yes |

The two capabilities this product depends on most for sounding human —
**affective dialog** and **`NON_BLOCKING` function calling** — are both absent
from Google's *newer* Live model. `NON_BLOCKING` is what keeps the line from
going silent during a 200-800 ms calendar lookup. Losing it is not cosmetic.

The 2.5 native-audio model page carries **no deprecation notice**. A
third-party source claimed it was deprecated; Google's own model documentation
does not. This should be re-verified before go-live.

**Fallback if 2.5 is retired:** switch to `gemini-3.1-flash-live-preview`,
accepting degraded emotional range, and compensate for the loss of
`NON_BLOCKING` by emitting a **pre-recorded filler clip** into the outbound
audio queue the instant a tool call starts. `ai/session.py` isolates this
choice so the fallback is a config change plus one filler path.

Endpoint: `wss://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent`

---

## 3. Architecture

```
                    ┌─────────────────────────────────────────┐
   PSTN caller ────▶│ Telnyx number                           │
                    └──────────────┬──────────────────────────┘
                                   │ webhook: call.initiated
                                   ▼
                    ┌─────────────────────────────────────────┐
                    │ FastAPI                                 │
                    │  POST /telnyx/webhook  → answer + stream │
                    │  WS   /telnyx/stream   → audio bridge    │
                    └──────────────┬──────────────────────────┘
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        │                  CallSession (bridge)                │
        │                                                      │
        │   Telnyx WS  ──16 kHz L16, base64──▶  Gemini Live    │
        │   Telnyx WS  ◀──16 kHz L16, base64──  (24k→16k)      │
        │                                                      │
        │   interrupted ──▶ {"event":"clear"} + flush queue     │
        │   ~10 min reset ──▶ session resumption handle         │
        └──────────────────────────┬──────────────────────────┘
                                   │ function calls
                                   ▼
        ┌─────────────────────────────────────────────────────┐
        │ Scheduling core (pure, synchronous, unit-testable)   │
        │   find_slots · book · reschedule · cancel · lookup   │
        └──────────────┬──────────────────────────────────────┘
                       │
                       ▼
        ┌─────────────────────────────────────────────────────┐
        │ Postgres — source of truth                          │
        └──────┬─────────────────┬────────────────┬───────────┘
               │                 │                │
       Calendar mirror    Reminder worker    Dashboard
       (service acct)     T-24h → SMS        calls/appts
```

### Why the DB is the source of truth, not Google Calendar

- **Latency.** Slot lookup is a ~1 ms indexed query vs a 200–500 ms Google API
  round-trip. On a live call this is the difference between natural and dead air.
- **Correctness.** Postgres gives transactional booking with an exclusion
  constraint that makes double-booking impossible. Google Calendar has no such
  guarantee and will race.
- **Domain fit.** Google Calendar has no native concept of appointment-type
  duration, buffers, or provider eligibility. Encoding those in event
  descriptions is fragile.

Google Calendar is a **one-way mirror for human visibility**, written via a
**service account** the clinic calendar is shared with. This requires no OAuth
consent screen and no Google app verification.

ICS feed subscription was evaluated and rejected: Google throttles subscribed
feeds to an **8–24 hour** refresh with no manual override.

---

## 4. Module boundaries

Each module has one job, a narrow interface, and is testable alone.

| Module | Responsibility | Depends on |
|---|---|---|
| `telephony/telnyx_client.py` | Call Control REST: answer, transfer, hangup, stream start | Telnyx API |
| `telephony/stream.py` | Telnyx WS framing: parse/emit `media`, `clear`, `mark`, `dtmf` | — |
| `ai/provider.py` | Returns a configured Live API client. **The AI Studio ↔ Vertex seam.** | google-genai |
| `ai/session.py` | Live session lifecycle: config, resumption, compression, reconnect | `ai/provider` |
| `audio/resample.py` | 24 kHz → 16 kHz PCM16, 20 ms framing | numpy/scipy |
| `bridge/call_session.py` | Orchestrates the two WS, barge-in, tool dispatch, timers | all above |
| `scheduling/slots.py` | **Pure function.** Availability → bookable slots | — |
| `scheduling/booking.py` | Transactional book/reschedule/cancel | Postgres |
| `agent/tools.py` | Function-calling schemas + handlers | `scheduling` |
| `agent/persona.py` | System instruction, greeting, guardrails | config |
| `calendar/mirror.py` | Push appointments to Google Calendar | service account |
| `reminders/worker.py` | T-24h scan → SMS, idempotent | Telnyx Messaging |
| `messaging/inbound.py` | Inbound SMS webhook: **STOP/START opt-out**, `C` to cancel | `scheduling/booking` |
| `web/dashboard.py` | Calls, transcripts, appointments, reminder status | Postgres |

`scheduling/slots.py` being **pure** is deliberate: it is the most logic-dense
and bug-prone part of the system, and it can be exhaustively unit-tested with
no database, no network, and no mocks.

---

## 5. Data model

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

providers (
  id, name, active
)

appointment_types (
  id, name, duration_min, buffer_before_min, buffer_after_min,
  description, active
)

-- which providers can perform which appointment types
appointment_type_providers (appointment_type_id, provider_id)

-- recurring weekly availability
availability_rules (
  id, provider_id, weekday SMALLINT,   -- 0=Mon .. 6=Sun
  start_time TIME, end_time TIME
)

-- dated overrides: holidays, half-days, one-off extra hours
availability_exceptions (
  id, provider_id, on_date DATE,
  start_time TIME NULL, end_time TIME NULL,
  is_closed BOOLEAN
)

patients (          -- SYNTHETIC ONLY in Phase 1
  id, name, phone, created_at
)

appointments (
  id, provider_id, appointment_type_id, patient_id,
  starts_at TIMESTAMPTZ, ends_at TIMESTAMPTZ,
  status,               -- booked | cancelled | completed | no_show
  source,               -- voice_agent | manual
  gcal_event_id, created_at,

  -- makes double-booking structurally impossible
  EXCLUDE USING gist (
    provider_id WITH =,
    tstzrange(starts_at, ends_at) WITH &&
  ) WHERE (status = 'booked')
)

calls (
  id, telnyx_call_control_id, from_number,
  started_at, ended_at, outcome, transcript, disclosure_spoken_at
)

reminders (
  id, appointment_id UNIQUE,   -- UNIQUE gives send-once idempotency
  scheduled_for, sent_at, status, provider_message_id
)
```

Two constraints carry real weight:

- The **GiST exclusion constraint** on `appointments` means a double-booking is
  rejected by the database itself, even under concurrent calls. This is stronger
  than application-level checking.
- **`reminders.appointment_id UNIQUE`** means the reminder worker can crash,
  retry, or run twice without ever double-texting a patient.

---

## 6. Slot generation algorithm

`find_slots(appointment_type_id, provider_id | None, date_range) -> [Slot]`

1. Resolve candidate providers (those eligible for the appointment type).
2. For each provider and each date in range, expand `availability_rules` for
   that weekday into intervals.
3. Apply `availability_exceptions`: `is_closed` removes the day; otherwise
   replace that day's intervals with the exception window.
4. Subtract existing `booked` appointments, **each widened by its own
   `buffer_before` and `buffer_after`**.
5. Walk each remaining free interval in `granularity_min` steps, emitting a
   start time wherever `duration + buffers` fits entirely inside the interval.
6. Filter out starts earlier than `now + min_lead_time_min`, and later than
   `now + booking_horizon_days`.
7. Sort by start time, return the first N (default 3).

**Timezone handling:** all storage in UTC (`TIMESTAMPTZ`); all availability
rules and all speech in the clinic's local timezone. Conversion happens at the
boundary. DST transitions are an explicit test case — a naive implementation
silently produces wrong slots twice a year.

**Concurrency:** booking re-validates the slot inside the transaction that
inserts it. The exclusion constraint is the backstop.

---

## 7. Voice pipeline

### Call setup

1. `call.initiated` webhook → verify Telnyx signature → `answer` the call.
2. Issue `streaming_start` with:
   - `stream_url`: our `wss://.../telnyx/stream`
   - `stream_bidirectional_mode: "rtp"`
   - `stream_bidirectional_codec: "L16"`
   - `stream_track: "inbound_track"`
3. Telnyx opens the WS and sends `connected`, then `start` (carries
   `call_control_id` and `media_format`).
4. On `start`, open the Gemini Live session and speak the greeting.

### Audio flow

- **Inbound:** Telnyx `media.payload` → base64-decode → **send as-is**.
  Both sides are 16 kHz PCM16 LE, so there is no resampling on the hot path.
- **Outbound:** Gemini 24 kHz PCM16 → `resample_poly(up=2, down=3)` → 16 kHz →
  slice into **20 ms frames (320 samples / 640 bytes)** → base64 → Telnyx.
- Outbound frames are **paced** to real time rather than flushed at once, so
  that a barge-in can actually cut the remaining audio.

### Barge-in

On Gemini `server_content.interrupted`:
1. Clear the local outbound frame queue.
2. Send `{"event": "clear"}` to Telnyx, which drops already-buffered playback.

Without step 2 the caller keeps hearing several hundred milliseconds of speech
after they started talking, which is the single most robot-like failure mode.

### The ~10 minute reset

Gemini resets the connection around 10 minutes and caps uncompressed audio
sessions at 15 minutes. Both are shorter than a bad phone call.

- `context_window_compression` with a sliding window and a trigger around
  25.6k tokens → unbounded session length.
- `session_resumption`: store the handle from every `SessionResumptionUpdate`;
  on disconnect, reconnect with it. Conversation state survives.
- Telnyx audio is buffered during the reconnect gap rather than dropped.
- If two consecutive resumptions fail: apologize once, offer transfer.

### Latency budget

Target **< 800 ms** mouth-to-ear. Instrument each hop and log per turn:

| Hop | Budget |
|---|---|
| Telnyx → us | ~50 ms |
| Gemini VAD + inference to first audio | ~400 ms |
| Resample + frame | < 10 ms |
| Us → Telnyx → caller | ~50 ms |
| Headroom | ~290 ms |

Tool calls are the main threat to this, which is why:

### Perceived-latency handling

A calendar lookup takes 200–800 ms. Silence in that gap reads as broken.

1. **`behavior: NON_BLOCKING`** function calling (supported on the 2.5 native
   audio model) lets the model keep speaking while the tool runs.
2. The system instruction requires a spoken bridge before any tool call
   ("let me take a look at that for you") so the line is never dead.

---

## 8. Agent tools

| Tool | Arguments | Returns |
|---|---|---|
| `find_slots` | `appointment_type`, `provider?`, `date_preference?` | up to 3 slots, spoken-friendly |
| `book_appointment` | `slot_token`, `patient_name`, `patient_phone` | confirmation + reference |
| `lookup_appointment` | `patient_phone?`, `patient_name?` | matching upcoming appointments |
| `reschedule_appointment` | `appointment_id`, `new_slot_token` | new confirmation |
| `cancel_appointment` | `appointment_id` | cancellation confirmation |
| `answer_faq` | `question` | answer from the knowledge file |
| `transfer_to_human` | `reason` | triggers Telnyx transfer |

`slot_token` is an opaque, short-lived token issued by `find_slots` that encodes
the exact provider/time/type. The model never constructs a booking time from
free text — it can only book a slot the scheduler actually offered. This removes
an entire class of hallucinated-appointment bugs.

---

## 9. Persona, disclosure, and guardrails

### Greeting (discloses in the first sentence, well inside 15 s)

> "Thanks for calling {clinic_name} — I'm an AI assistant, and I can book,
> move, or cancel an appointment for you. What can I do for you today?"

### Voice character

Native-audio model with `enable_affective_dialog: true`. System instruction
tuned for: contractions, short turns (one or two sentences), natural
acknowledgements, asking one question at a time, and never reading long lists —
offering at most three options aloud.

### Hard guardrails

These are enforced in the system instruction **and** by the absence of any tool
that could do otherwise:

- **Never give medical advice**, interpret symptoms, or discuss test results.
- **Never claim or imply clinical credentials** (CA AB 489).
- Any symptom or clinical question → acknowledge, then `transfer_to_human`.
- Any hint of emergency → "Please hang up and call 911 right away."
- If the caller asks whether it is an AI, answer plainly yes (Utah AI Policy Act).
- Never invent hours, prices, providers, or availability. Unknown → FAQ or transfer.

---

## 10. Reminders

- Worker runs every 15 minutes.
- Selects `booked` appointments starting in 23–25 h with no `reminders` row.
- Inserts the `reminders` row **first** (UNIQUE constraint = send-once), then sends.
- Message is deliberately minimal, with no clinical content:

  > "Reminder: you have an appointment at {clinic} tomorrow at {time}.
  > Reply C to cancel or call {phone}. Reply STOP to opt out."

- **STOP / opt-out handling is mandatory** and honored before every send.
  Inbound SMS is handled by `messaging/inbound.py` on a Telnyx messaging
  webhook: `STOP`/`UNSUBSCRIBE` sets an opt-out flag checked before every send,
  `START` clears it, and `C` cancels the referenced appointment and replies with
  a confirmation. Opt-out is a legal requirement, not a nicety — the reminder
  copy promises both replies, so both must actually work.
- **Blocked until 10DLC brand + campaign are approved.** Until then the worker
  runs in dry-run mode, writing what it would have sent to the dashboard.

---

## 11. Dashboard (minimal)

Single FastAPI app behind basic auth:

- `/calls` — recent calls, duration, outcome, transcript, latency per turn
- `/appointments` — upcoming, with provider and type
- `/reminders` — scheduled / sent / dry-run

Its real purpose is **debugging what the bot actually said**, which is the
fastest way to improve call quality.

---

## 12. Failure modes and handling

| Failure | Handling |
|---|---|
| Gemini WS drops | Resume with handle. Two failures → apologize, offer transfer |
| Tool raises | Return a structured error; model apologizes and offers transfer |
| No slots available | Offer next available date, or transfer |
| Caller silent | Re-prompt at 6 s; after two, offer transfer then hang up gracefully |
| Caller presses `0` | Immediate transfer (DTMF handled independently of the model) |
| Telnyx stream never starts | Fall back to Telnyx TTS message + transfer |
| Double-book attempt | Exclusion constraint rejects; model offers the next slot |
| Reminder send fails | Row stays `pending`; retried with backoff, never duplicated |

---

## 13. Testing strategy

**Unit (no network, no DB):**
- `scheduling/slots.py` — table-driven: buffers, back-to-back bookings, closures,
  half-days, lead time, horizon, **DST spring-forward and fall-back**
- `audio/resample.py` — length, sample rate, no clipping, exact 20 ms framing

**Integration:**
- Fake Telnyx WS client that replays recorded audio and asserts on emitted frames
- Fake Gemini session that emits scripted transcripts, tool calls, and
  `interrupted` events
- Barge-in test: assert `{"event":"clear"}` is emitted and the queue is flushed
- Session-resumption test: kill the socket mid-turn, assert context survives
- Booking concurrency: two simultaneous bookings of one slot → exactly one wins

**Contract:**
- Assert Telnyx accepts our `streaming_start` payload
- Assert Gemini accepts our Live config (affective dialog, compression, tools)

**Manual:**
- Real call to the number, scripted scenarios: book, reschedule, cancel, FAQ,
  interrupt mid-sentence, ask for a human, go silent, ask "are you a robot?"

---

## 14. Configuration

`config.yaml` — everything clinic-specific, so nothing is hardcoded:

```yaml
clinic:
  name: "..."
  timezone: "America/New_York"
  phone_human: "+1..."
providers: [...]
appointment_types:
  - {name: "New patient", duration_min: 45, buffer_after_min: 10}
  - {name: "Follow-up",   duration_min: 15, buffer_after_min: 5}
  - {name: "Procedure",   duration_min: 60, buffer_before_min: 10, buffer_after_min: 15}
slot_policy:
  granularity_min: 15
  min_lead_time_min: 120
  booking_horizon_days: 60
faq: [...]
```

`.env` — `TELNYX_API_KEY`, `TELNYX_NUMBER`, `GEMINI_API_KEY`, `DATABASE_URL`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `AI_PROVIDER=ai_studio|vertex`, `DASHBOARD_PASSWORD`.

---

## 15. Deployment

- **Fly.io**, single always-on machine (long-lived WS needs a persistent process;
  serverless is a poor fit).
- Managed Postgres (Fly Postgres or Supabase).
- TLS terminated by the platform → `wss://` for Telnyx.
- Health check endpoint; structured JSON logs.
- Local development via ngrok tunnel to the same webhook + WS routes.

---

## 16. Deferred to go-live (not built in Phase 1)

- Vertex AI provider + Google Cloud BAA
- Telnyx BAA
- 10DLC registration
- Transcript encryption at rest + retention policy
- PHI scrubbing from application logs
- Audit trail of PHI access
- Call recording consent flow

---

## 17. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Latency makes it feel robotic | High | Latency budget instrumented per turn; NON_BLOCKING tools + spoken bridges |
| Affective dialog is preview-only and may change | Medium | Isolated in `ai/session.py` config; degrades to standard native audio |
| Real patient calls the prototype | **High** | Explicit boundary in this spec; do not publish the number until Vertex + BAA |
| DST bug produces wrong slots | Medium | Explicit unit tests both directions |
| 10DLC rejection delays reminders | Medium | Dry-run mode ships first; voice works without SMS |
| Model hallucinates an appointment | Medium | `slot_token` — model can only book slots the scheduler issued |
| 2.5 native-audio model retired | **High** | Both differentiating features are exclusive to it. Fallback to 3.1 + pre-recorded filler documented in 2.1; re-verify status before go-live |

---

## 18. Build order

1. Scheduling core + tests (pure logic, no I/O — highest bug density, cheapest to test)
2. Telnyx call answer + echo-back audio (proves the transport)
3. Gemini Live bridge + barge-in (proves the conversation)
4. Tools wired to scheduling (proves the product)
5. Session resumption + compression (proves it survives a real call)
6. Calendar mirror
7. Reminder worker (dry-run)
8. Dashboard
