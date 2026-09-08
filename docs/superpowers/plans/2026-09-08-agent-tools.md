# Agent Tools Implementation Plan (Plan 3 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the agent's declared tools to the Plan 1 scheduling core, so a caller can actually book, move, and cancel appointments and get questions answered.

**Architecture:** A `ToolRouter` object owns the per-call state that tools need — chiefly the option-number → slot-token registry — and dispatches by name. It is the only place that knows both the model's vocabulary and the scheduler's API. Every known failure comes back as a *structured result* the model can speak, never an exception.

**Tech Stack:** Python 3.12, asyncpg, the Plan 1 scheduling core, the Plan 2 bridge.

---

## Gap this plan must close first

**The model does not know today's date.** The system instruction from Plan 2 describes
the clinic but never says what day it is, so "tomorrow", "next Tuesday" and "this week"
are unanswerable. A scheduling agent that cannot resolve a relative date is not usable.

Two changes follow from that:

1. The system instruction gains the **current clinic-local date and time**, injected at
   session start.
2. `find_slots` stops taking a free-text `date_preference` and takes **structured
   hints** instead: `earliest_date` (`YYYY-MM-DD`) and `part_of_day`. Models are good at
   turning "next Tuesday morning" into structured values *once they know today's date*;
   they are unreliable at it otherwise, and a free-text field pushes the parsing problem
   into our code where it does not belong.

---

## Design decisions

**Option numbers, not tokens.** `find_slots` returns `{option: 1, when: "...", provider: "..."}`
and keeps the signed tokens in per-call state. The model books by integer. Tokens never
enter the model's context: shorter prompts, nothing to garble, and the hallucination
guarantee from Plan 1 still holds because only a token the scheduler issued can book.

**Caller ID is the default phone number.** Asking a caller to recite a number they are
calling from is the kind of thing that makes an agent feel robotic. `book_appointment`
uses `ctx.caller_number` unless the caller offers a different one.

**Known failures are results, not exceptions.** `SlotTaken`, `AppointmentNotFound`, and an
unrecognised option number all return `{"error": ..., "recovery": ...}`. The model then
apologises and re-offers. Only genuinely unexpected errors propagate, where the bridge
already turns them into a transfer offer.

**Part-of-day boundaries** are fixed and explicit so tests are unambiguous:
morning `< 12:00`, afternoon `12:00–16:59`, evening `>= 17:00`, all clinic-local.

---

## File structure

| File | Responsibility |
|---|---|
| `agent/tools.py` | `ToolRouter`, per-call state, all seven handlers |
| `agent/faq.py` | Match a spoken question to a configured FAQ entry |
| `scheduling/booking.py` | + `find_upcoming_appointments` read query |
| `ai/live_config.py` | + current date/time in the instruction; structured `find_slots` args |
| `tests/test_agent_tools.py` | Router tests against real Postgres |
| `tests/test_faq.py` | FAQ matching, pure |

---

### Task 1: FAQ matching

**Files:** Create `src/clinic_agent/agent/faq.py`, `tests/test_faq.py`

- [ ] **Step 1: Tests** — an exact question matches; a paraphrase matches
      ("where are you" → the location entry); an unrelated question returns `None`
      rather than the closest bad guess; matching ignores case and punctuation.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement** with token-overlap scoring plus a minimum threshold, so a
      question we cannot answer is refused rather than answered wrongly. Answering a
      medical question with a parking answer is worse than admitting ignorance.
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 2: Upcoming-appointment lookup

**Files:** Modify `src/clinic_agent/scheduling/booking.py`, `tests/test_booking.py`

- [ ] **Step 1: Tests** — finds a booked appointment by phone; ignores cancelled ones;
      ignores appointments in the past; matches by name case-insensitively; returns
      soonest first.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement `find_upcoming_appointments(pool, *, phone=None, name=None, now)`.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 3: Structured slot hints and the current date

**Files:** Modify `src/clinic_agent/ai/live_config.py`, `src/clinic_agent/scheduling/service.py`, `tests/test_live_config.py`

- [ ] **Step 1: Tests** — the instruction contains today's clinic-local date;
      `find_slots` declares `earliest_date` and `part_of_day` and no longer declares
      `date_preference`; `part_of_day` is an enum; `available_slots` filters to morning
      slots only; `earliest_date` moves the search window.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 4: The tool router

**Files:** Create `src/clinic_agent/agent/tools.py`, `tests/test_agent_tools.py`

- [ ] **Step 1: Tests** (against real Postgres and the shipped `config.yaml`)
  - `find_slots` returns numbered options and records tokens in call state
  - `book_appointment` with a valid option writes the appointment
  - booking uses the caller's number by default, and an override when given
  - an unrecognised option number returns an error and books nothing
  - a slot taken between offer and booking returns a structured error, not an exception
  - `lookup_appointment` finds the caller's booking from caller ID alone
  - `reschedule_appointment` moves it and keeps its identity
  - `cancel_appointment` frees the slot; cancelling twice errors cleanly
  - `answer_faq` answers a known question and refuses an unknown one
  - `transfer_to_human` calls Telnyx with the configured number
  - an unknown tool name returns an error rather than raising
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 5: Wire it into the app

**Files:** Modify `src/clinic_agent/main.py`, `src/clinic_agent/app.py`

- [ ] **Step 1:** Pass a `ToolRouter` as the bridge's `tool_handler`.

**Revision to this plan:** it said to build a router *per call* because the router
holds per-call state. It does not. All per-call state lives on the `ToolContext` the
bridge already creates when a call starts, which makes `ToolRouter` stateless and
safe to share across every concurrent call. That is strictly simpler, and it is what
lets the end-to-end test prove state survives between two tool calls on one context.
- [ ] **Step 2:** Remove the `not_wired_yet` placeholder.
- [ ] **Step 3:** Add a DB pool to app startup, seeded from config.
- [ ] **Step 4:** Full suite green, lint clean. Commit.

---

## Definition of done -- COMPLETE

- [x] A caller can book, reschedule, cancel, and ask a question end to end in tests,
      including a scripted call that drives tool calls through the audio bridge into
      Postgres
- [x] The model knows today's date, in clinic-local time
- [x] Every known failure is a spoken-recoverable result, not an exception
- [x] Tokens never enter the model's context (asserted directly)
- [x] Full suite green (**224 tests**), lint clean

**Delivered beyond the plan:**
- **Authorisation on reschedule and cancel.** Only appointment ids that
  `lookup_appointment` returned *for this caller* can be changed. Without it a
  hallucinated or guessed id could move or cancel a stranger's appointment. Two
  tests act as the attacker.
- **Name-only lookup removed from the tool schema.** Matching a spoken name alone
  would hand out another patient's details; lookup is caller-ID-first.
- **Offers are consumed on booking**, so one call cannot book the same slot twice.
- **Preference filtering runs before the offer limit**, fixing a bug where asking for
  the afternoon would return nothing whenever the first three slots were mornings.
- **One spoken-time formatter.** The offer path and the read-back path had drifted --
  "the 14th" versus "the 14", the latter spoken as "the fourteen". Now shared, with
  ordinal tests covering 11th/12th/13th/21st/22nd.

## Deferred

Calendar mirror and SMS reminders (Plan 4), dashboard (Plan 5), and verification
against the real Telnyx and Gemini APIs, which needs credentials.
