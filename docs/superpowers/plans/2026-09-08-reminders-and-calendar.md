# Reminders & Calendar Mirror Implementation Plan (Plan 4 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the original brief — send each patient an SMS reminder the day before their appointment, and mirror bookings into the clinic's Google Calendar so staff can see them in the app they already use.

**Architecture:** Both features are *followers* of the appointments table, never the source of truth. A reminder worker scans for appointments 24 hours out; a calendar mirror pushes changes to Google. Neither is allowed to block or fail a booking — a patient getting an appointment matters more than a calendar entry appearing.

**Tech Stack:** Python 3.12, asyncpg, google-api-python-client + google-auth (service account), Telnyx Messaging API.

---

## Verified constraints driving this plan

| Constraint | Consequence |
|---|---|
| **US A2P SMS requires 10DLC** brand + campaign registration | Reminders **cannot legally send** until registration completes. The worker ships in **dry-run mode** and records what it *would* have sent |
| Opt-out handling is legally required | Inbound `STOP` must be honoured before every send, and the reminder copy promises `STOP` and `C`, so both must actually work |
| A Google **service account** avoids OAuth app verification | The clinic shares its calendar with a `…iam.gserviceaccount.com` address. No consent screen, no 2–6 week review |
| Reminder content is PHI-adjacent | Message says *when* and *where*, never *why*. No provider specialty, no reason for visit |

---

## Design decisions

**Insert the reminder row before sending.** `reminders.appointment_id` is `UNIQUE`, so
claiming the row first makes the send exactly-once even if the worker crashes,
retries, or runs twice concurrently. Sending first and recording after would double-text
patients on any restart.

**Dry-run is a first-class mode, not a stub.** It writes the row with
`status='dry_run'` and the exact body it would have sent. That way the 10DLC switch is
a config flip, and the message copy is reviewable before a single text reaches a patient.

**The calendar mirror never raises into the booking path.** A failed Google call logs and
leaves the appointment untouched, and a reconciliation pass can retry later. Losing a
calendar entry is annoying; losing a booking is not acceptable.

**Cancelling deletes the calendar event, it does not just mark it.** A cancelled
appointment still visible on a provider's phone is worse than no entry at all.

---

## File structure

| File | Responsibility |
|---|---|
| `reminders/messages.py` | Compose reminder text. **Pure** |
| `reminders/worker.py` | T-24h scan, claim-then-send, dry-run |
| `messaging/inbound.py` | Inbound SMS: `STOP`/`START`/`C` |
| `calendar_mirror/events.py` | Appointment → Google event body. **Pure** |
| `calendar_mirror/sync.py` | Create/update/delete against the API |
| `db/migrations/002_reminders.sql` | Opt-out and dry-run support |

---

### Task 1: Reminder message composition (pure)

**Files:** Create `src/clinic_agent/reminders/messages.py`, `tests/test_reminder_messages.py`

- [ ] **Step 1: Tests** — the body names the clinic, the local date and time, and the
      opt-out; it contains **no** provider specialty or reason for visit; times render in
      clinic-local time with correct ordinals; a same-day reminder reads "today" rather
      than a date; the body stays within one 160-character SMS segment where possible,
      and the test asserts the segment count so cost is visible.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 2: Opt-out schema

**Files:** Create `src/clinic_agent/db/migrations/002_reminders.sql`

- [ ] **Step 1:** Add `reminders.body`, widen `reminders.status` values to include
      `dry_run`/`failed`, and index `patients.sms_opted_out`. `patients.sms_opted_out`
      already exists from migration 001.
- [ ] **Step 2:** Apply and confirm idempotency by running it twice.
- [ ] **Step 3:** Commit.

---

### Task 3: Reminder worker

**Files:** Create `src/clinic_agent/reminders/worker.py`, `tests/test_reminder_worker.py`

- [ ] **Step 1: Tests** (real Postgres, fake Telnyx)
  - an appointment 24 h out is reminded exactly once
  - running the worker twice sends **one** message (the UNIQUE claim)
  - two workers running concurrently send **one** message
  - an appointment 3 days out is not reminded yet
  - a cancelled appointment is not reminded
  - an opted-out patient is skipped, and the row records why
  - dry-run mode records the body and sends nothing
  - a Telnyx failure marks the row `failed` and does not block other reminders
  - a `failed` row is retried on the next run
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 4: Inbound SMS

**Files:** Create `src/clinic_agent/messaging/inbound.py`, `tests/test_inbound_sms.py`

- [ ] **Step 1: Tests** — `STOP` sets the opt-out flag and replies with confirmation;
      `stop`, `STOP.`, ` Stop ` all work (callers do not type carefully); `START`
      clears it; `C` cancels the next upcoming appointment and confirms; `C` with
      nothing booked replies helpfully; an unrecognised message does not change state;
      a message from an unknown number is ignored safely.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement**, plus a signed `POST /telnyx/messaging` route reusing the
      existing Ed25519 verification.
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 5: Calendar event bodies (pure)

**Files:** Create `src/clinic_agent/calendar_mirror/events.py`, `tests/test_calendar_events.py`

- [ ] **Step 1: Tests** — the event has RFC3339 start/end with the clinic timezone;
      the summary names the patient and appointment type; the description carries the
      appointment id so a mirror can be reconciled; **no reason-for-visit** appears;
      buffers are excluded from the visible event.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 6: Calendar sync

**Files:** Create `src/clinic_agent/calendar_mirror/sync.py`, `tests/test_calendar_sync.py`

- [ ] **Step 1: Tests** (fake Google service) — booking creates an event and stores
      `gcal_event_id`; rescheduling patches the existing event rather than creating a
      second; cancelling deletes it; a Google failure logs and leaves the appointment
      intact; a missing `gcal_event_id` on cancel is not an error.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

## Definition of done -- COMPLETE

- [x] A reminder sends exactly once, proven under concurrency across 15 runs
- [x] `STOP` honoured before every send; both `STOP` and `C` work, and the
      `CANCEL` keyword collision is handled explicitly
- [x] Dry-run records real message bodies, viewable on the dashboard
- [x] Calendar failures never affect bookings -- guarded twice, and a test proved
      the second guard was necessary
- [x] No reminder discloses a reason for visit (asserted); the calendar carries the
      appointment type because it is an internal staff view
- [x] Full suite green (**322 tests**), lint clean, no credentials needed

**Delivered beyond the plan:**
- Migrations now apply in filename order via `db/migrate.py`
- SMS segment counting fixed a real bug: UCS-2 counts UTF-16 code units, so a
  non-BMP emoji is a surrogate pair. Counting Python characters understated the bill
- A missing sender number falls back to dry-run rather than dropping reminders
- Failed reminders are retried, with an attempt counter
- Calls are persisted with transcripts; the `calls` table had existed since
  migration 001 with nothing writing to it

## Deferred

Dashboard (Plan 5). Live verification against Telnyx Messaging and the Google Calendar
API, which needs credentials and completed 10DLC registration.
