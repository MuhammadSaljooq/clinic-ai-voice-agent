"""Reminder worker against real Postgres.

The headline property is send-exactly-once, including under concurrency and across a
crash. Double-texting patients is the failure that actually costs a clinic trust.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.reminders.worker import run_once

REPO = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
FROM_NUMBER = "+15550222"


class FakeTelnyx:
    def __init__(self, fail_for: set[str] | None = None):
        self.sent: list[dict] = []
        self.fail_for = fail_for or set()

    async def send_sms(self, *, to, from_, text):
        if to in self.fail_for:
            raise RuntimeError(f"carrier rejected {to}")
        self.sent.append({"to": to, "from": from_, "text": text})
        return {"data": {"id": f"msg-{len(self.sent)}"}}


@pytest.fixture
async def clinic_db(pool):
    cfg = load_config(REPO / "config.yaml")
    cfg.reminders.enabled = True
    cfg.reminders.dry_run = False
    await seed_from_config(pool, cfg)
    return pool, cfg


async def add_appointment(
    pool, *, hours_from_now: float, phone="+15550100", name="Ada", status="booked",
    opted_out=False,
):
    patient_id = await pool.fetchval(
        """
        INSERT INTO patients (name, phone, sms_opted_out) VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE SET sms_opted_out = EXCLUDED.sms_opted_out
        RETURNING id
        """,
        name, phone, opted_out,
    )
    starts = NOW + timedelta(hours=hours_from_now)
    return await pool.fetchval(
        """
        INSERT INTO appointments (provider_id, appointment_type_id, patient_id,
                                  starts_at, ends_at, blocked_range, status)
        VALUES (1, 1, $1, $2, $3, tstzrange($2, $3), $4)
        RETURNING id
        """,
        patient_id, starts, starts + timedelta(minutes=15), status,
    )


async def status_of(pool, appointment_id):
    return await pool.fetchrow(
        "SELECT status, body, attempts, provider_message_id, last_error"
        " FROM reminders WHERE appointment_id = $1",
        appointment_id,
    )


# --- the core guarantee --------------------------------------------------------


async def test_an_appointment_inside_the_window_is_reminded_once(clinic_db):
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20)
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert run.sent == 1
    assert len(telnyx.sent) == 1
    row = await status_of(pool, appointment)
    assert row["status"] == "sent"
    assert row["provider_message_id"] == "msg-1"
    assert cfg.clinic.name in row["body"]


async def test_running_the_worker_twice_sends_only_one_message(clinic_db):
    """A redeploy or a cron overlap must not double-text anyone."""
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=20)
    telnyx = FakeTelnyx()

    await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    second = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert len(telnyx.sent) == 1
    assert second.considered == 0


async def test_two_workers_running_at_once_send_only_one_message(clinic_db):
    """The UNIQUE claim is what makes this safe, not luck."""
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=20)
    telnyx = FakeTelnyx()

    runs = await asyncio.gather(
        run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW),
        run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW),
    )

    assert len(telnyx.sent) == 1, "a patient received two reminders"
    assert sum(r.sent for r in runs) == 1
    assert await pool.fetchval("SELECT count(*) FROM reminders") == 1


# --- selection window ----------------------------------------------------------


async def test_an_appointment_beyond_the_window_is_not_reminded_yet(clinic_db):
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=72)
    telnyx = FakeTelnyx()
    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    assert run.considered == 0
    assert telnyx.sent == []


async def test_an_appointment_already_in_the_past_is_never_reminded(clinic_db):
    """Catching up after downtime must not text people about yesterday."""
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=-5)
    run = await run_once(pool, cfg, telnyx=FakeTelnyx(), from_number=FROM_NUMBER, now=NOW)
    assert run.considered == 0


async def test_a_cancelled_appointment_is_not_reminded(clinic_db):
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=20, status="cancelled")
    run = await run_once(pool, cfg, telnyx=FakeTelnyx(), from_number=FROM_NUMBER, now=NOW)
    assert run.considered == 0


# --- opt-out -------------------------------------------------------------------


async def test_an_opted_out_patient_is_skipped_and_the_reason_recorded(clinic_db):
    """Legally required, and checked per send -- they may have replied STOP after
    the appointment was booked."""
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20, opted_out=True)
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert telnyx.sent == [], "texted a patient who opted out"
    assert run.skipped_opted_out == 1
    assert (await status_of(pool, appointment))["status"] == "skipped_opted_out"


# --- dry run -------------------------------------------------------------------


async def test_dry_run_records_the_exact_body_and_sends_nothing(clinic_db):
    """Ships before 10DLC approval so the wording can be reviewed for real."""
    pool, cfg = clinic_db
    cfg.reminders.dry_run = True
    appointment = await add_appointment(pool, hours_from_now=20)
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert telnyx.sent == []
    assert run.dry_run == 1
    row = await status_of(pool, appointment)
    assert row["status"] == "dry_run"
    assert "reminder of your appointment" in row["body"]


async def test_without_a_sender_number_it_falls_back_to_dry_run(clinic_db):
    """Misconfiguration should not silently drop reminders."""
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=20)
    run = await run_once(pool, cfg, telnyx=FakeTelnyx(), from_number=None, now=NOW)
    assert run.dry_run == 1


# --- failure handling ----------------------------------------------------------


async def test_one_failed_send_does_not_stop_the_others(clinic_db):
    pool, cfg = clinic_db
    doomed = await add_appointment(pool, hours_from_now=18, phone="+15550001", name="A")
    fine = await add_appointment(pool, hours_from_now=20, phone="+15550002", name="B")
    telnyx = FakeTelnyx(fail_for={"+15550001"})

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert run.failed == 1
    assert run.sent == 1
    assert (await status_of(pool, doomed))["status"] == "failed"
    assert (await status_of(pool, fine))["status"] == "sent"


async def test_a_failed_reminder_is_retried_on_the_next_run(clinic_db):
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20, phone="+15550001")
    failing = FakeTelnyx(fail_for={"+15550001"})
    await run_once(pool, cfg, telnyx=failing, from_number=FROM_NUMBER, now=NOW)
    assert (await status_of(pool, appointment))["status"] == "failed"

    recovered = FakeTelnyx()
    run = await run_once(pool, cfg, telnyx=recovered, from_number=FROM_NUMBER, now=NOW)

    assert run.sent == 1
    row = await status_of(pool, appointment)
    assert row["status"] == "sent"
    assert row["attempts"] == 2, "attempts should count the retry"


async def test_a_sent_reminder_is_never_retried(clinic_db):
    pool, cfg = clinic_db
    await add_appointment(pool, hours_from_now=20)
    telnyx = FakeTelnyx()
    await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    assert len(telnyx.sent) == 1


# --- crash recovery ------------------------------------------------------------


async def test_a_lease_stuck_in_claimed_after_a_crash_is_recovered(clinic_db):
    """A worker that died between claiming and sending must not strand the reminder."""
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20)
    # Simulate the crash: a claim taken 30 minutes ago that never reached the network.
    await pool.execute(
        "INSERT INTO reminders (appointment_id, scheduled_for, status, attempts, updated_at)"
        " VALUES ($1, now(), 'claimed', 1, $2)",
        appointment, NOW - timedelta(minutes=30),
    )
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert run.sent == 1
    assert len(telnyx.sent) == 1
    row = await status_of(pool, appointment)
    assert row["status"] == "sent"
    assert row["attempts"] == 2, "the recovery counts as another attempt"


async def test_a_fresh_claim_is_left_alone(clinic_db):
    """A claim taken moments ago belongs to a worker that may still be sending; stealing
    it is exactly how a patient would get texted twice."""
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20)
    await pool.execute(
        "INSERT INTO reminders (appointment_id, scheduled_for, status, attempts, updated_at)"
        " VALUES ($1, now(), 'claimed', 1, $2)",
        appointment, NOW,  # just now -- still within the lease
    )
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert telnyx.sent == [], "a fresh claim must not be reclaimed"
    assert run.considered == 0
    assert (await status_of(pool, appointment))["status"] == "claimed"


async def test_a_message_in_sending_is_never_resent(clinic_db):
    """The ambiguous 'may already have gone out' state is left for a human, never auto-
    resent -- the whole design would rather miss a reminder than double one."""
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20)
    await pool.execute(
        "INSERT INTO reminders (appointment_id, scheduled_for, status, attempts, updated_at)"
        " VALUES ($1, now(), 'sending', 1, $2)",
        appointment, NOW - timedelta(hours=2),  # old, but still not eligible
    )
    telnyx = FakeTelnyx()

    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)

    assert telnyx.sent == []
    assert run.considered == 0


async def test_an_optout_skip_is_restored_after_the_patient_opts_back_in(clinic_db):
    pool, cfg = clinic_db
    appointment = await add_appointment(pool, hours_from_now=20, opted_out=True)
    telnyx = FakeTelnyx()

    await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    assert (await status_of(pool, appointment))["status"] == "skipped_opted_out"

    # Still opted out: a later run must not churn the row.
    again = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    assert again.considered == 0

    # They reply START, then a later run reminds them after all.
    await pool.execute("UPDATE patients SET sms_opted_out = FALSE WHERE phone = '+15550100'")
    run = await run_once(pool, cfg, telnyx=telnyx, from_number=FROM_NUMBER, now=NOW)
    assert run.sent == 1
    assert (await status_of(pool, appointment))["status"] == "sent"
