"""Send appointment reminders, exactly once.

The ordering is deliberate: **claim the row, then send.** `reminders.appointment_id`
is UNIQUE, so the insert is an atomic claim -- two workers racing, or one worker
restarted mid-run, cannot both get past it. Sending first and recording afterwards
would double-text patients on any crash or redeploy, which is the failure mode that
actually damages trust.

Selection is `now < starts_at <= now + hours_before`, which means the worker catches
up after downtime and never reminds anyone about an appointment that has already
passed.

Dry-run is a first-class mode rather than a stub: it records the exact text it would
have sent, so the wording is reviewable before a single message reaches a patient and
so the 10DLC switch is a config flip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from clinic_agent.config import ClinicConfig
from clinic_agent.reminders.messages import compose_reminder, sms_segments

log = logging.getLogger(__name__)

STATUS_CLAIMED = "claimed"
STATUS_SENT = "sent"
STATUS_DRY_RUN = "dry_run"
STATUS_FAILED = "failed"
STATUS_OPTED_OUT = "skipped_opted_out"


@dataclass
class ReminderRun:
    considered: int = 0
    sent: int = 0
    dry_run: int = 0
    skipped_opted_out: int = 0
    failed: int = 0
    lost_race: int = 0

    def __str__(self) -> str:
        return (
            f"considered={self.considered} sent={self.sent} dry_run={self.dry_run} "
            f"opted_out={self.skipped_opted_out} failed={self.failed} "
            f"lost_race={self.lost_race}"
        )


async def _candidates(pool: asyncpg.Pool, *, now: datetime, until: datetime):
    return await pool.fetch(
        """
        SELECT a.id AS appointment_id, a.starts_at,
               pt.phone, pt.sms_opted_out,
               r.id AS reminder_id, r.status AS reminder_status
        FROM appointments a
        JOIN patients pt ON pt.id = a.patient_id
        LEFT JOIN reminders r ON r.appointment_id = a.id
        WHERE a.status = 'booked'
          AND a.starts_at > $1
          AND a.starts_at <= $2
          AND (r.id IS NULL OR r.status = $3)
        ORDER BY a.starts_at
        """,
        now,
        until,
        STATUS_FAILED,
    )


async def _claim(pool: asyncpg.Pool, row, *, scheduled_for: datetime) -> int | None:
    """Take ownership of this reminder, or return None if someone else already has it."""
    if row["reminder_id"] is None:
        # UNIQUE(appointment_id) makes this the atomic claim.
        return await pool.fetchval(
            """
            INSERT INTO reminders (appointment_id, scheduled_for, status, attempts)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (appointment_id) DO NOTHING
            RETURNING id
            """,
            row["appointment_id"],
            scheduled_for,
            STATUS_CLAIMED,
        )
    # Retrying a previous failure: only one worker may flip it out of 'failed'.
    return await pool.fetchval(
        """
        UPDATE reminders
        SET status = $2, attempts = attempts + 1, updated_at = now()
        WHERE id = $1 AND status = $3
        RETURNING id
        """,
        row["reminder_id"],
        STATUS_CLAIMED,
        STATUS_FAILED,
    )


async def _finish(pool, reminder_id, status, *, body=None, message_id=None, error=None):
    await pool.execute(
        """
        UPDATE reminders
        SET status = $2,
            body = COALESCE($3, body),
            provider_message_id = COALESCE($4, provider_message_id),
            last_error = $5,
            sent_at = CASE WHEN $2 = 'sent' THEN now() ELSE sent_at END,
            updated_at = now()
        WHERE id = $1
        """,
        reminder_id,
        status,
        body,
        message_id,
        error,
    )


async def run_once(
    pool: asyncpg.Pool,
    cfg: ClinicConfig,
    *,
    telnyx=None,
    from_number: str | None = None,
    now: datetime | None = None,
) -> ReminderRun:
    now = now or datetime.now(UTC)
    until = now + timedelta(hours=cfg.reminders.hours_before)
    today_local = now.astimezone(cfg.tz).date()
    run = ReminderRun()

    for row in await _candidates(pool, now=now, until=until):
        run.considered += 1

        reminder_id = await _claim(pool, row, scheduled_for=now)
        if reminder_id is None:
            run.lost_race += 1
            continue

        body = compose_reminder(
            clinic_name=cfg.clinic.name,
            phone_display=cfg.clinic.phone_display,
            starts_at=row["starts_at"],
            tz=cfg.tz,
            today=today_local,
        )

        if row["sms_opted_out"]:
            # Checked per send, not once at sign-up: someone may have replied STOP
            # since the appointment was booked.
            await _finish(pool, reminder_id, STATUS_OPTED_OUT, body=body)
            run.skipped_opted_out += 1
            continue

        if cfg.reminders.dry_run or telnyx is None or not from_number:
            await _finish(pool, reminder_id, STATUS_DRY_RUN, body=body)
            run.dry_run += 1
            log.info(
                "[dry-run] would text %s (%d segment(s)): %s",
                row["phone"], sms_segments(body), body,
            )
            continue

        try:
            result = await telnyx.send_sms(to=row["phone"], from_=from_number, text=body)
        except Exception as exc:
            # One patient's failure must not stop everyone else's reminder.
            log.exception("reminder for appointment %s failed", row["appointment_id"])
            await _finish(pool, reminder_id, STATUS_FAILED, body=body, error=str(exc)[:500])
            run.failed += 1
            continue

        message_id = ((result or {}).get("data") or {}).get("id")
        await _finish(pool, reminder_id, STATUS_SENT, body=body, message_id=message_id)
        run.sent += 1

    if run.considered:
        log.info("reminder run: %s", run)
    return run
