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

**Crash recovery, without ever double-texting.** A claim is a lease. If a worker dies
between claiming a row and finishing it, the row would otherwise sit in `claimed`
forever and the patient would silently never be reminded. So a stale `claimed` lease
(older than `CLAIM_LEASE`) is reclaimed on a later run. The lease is only safe to
reclaim because the row is flipped to `sending` in a separate, durable write *before*
the Telnyx call -- so a stale `claimed` row provably never reached the network, while a
stale `sending` row (the ambiguous "did it go out?" case) is deliberately left alone
rather than risk a second text. That keeps the guarantee the whole design is built on:
a patient is never texted twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from clinic_agent.config import ClinicConfig
from clinic_agent.messaging import store
from clinic_agent.reminders.messages import compose_reminder, sms_segments

log = logging.getLogger(__name__)

STATUS_CLAIMED = "claimed"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_DRY_RUN = "dry_run"
STATUS_FAILED = "failed"
STATUS_OPTED_OUT = "skipped_opted_out"

# How long a claim is trusted before a later run may assume the worker that took it has
# died. Comfortably longer than a sweep so an in-progress send is never stolen.
CLAIM_LEASE = timedelta(minutes=15)


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


async def _candidates(pool: asyncpg.Pool, *, now: datetime, until: datetime, stale_before: datetime):
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
          AND (
                r.id IS NULL
                OR r.status = $3
                -- A lease that expired before it ever reached the network: the worker
                -- that held it died. 'sending' is intentionally excluded -- that one
                -- may already have gone out, and a second text is the one thing we
                -- never risk.
                OR (r.status = $4 AND r.updated_at < $5)
                -- Previously skipped because the patient had opted out, but they have
                -- since replied START. Opt-out is checked per send, so their consent
                -- returning should restore the reminder they would otherwise lose.
                OR (r.status = $6 AND NOT pt.sms_opted_out)
              )
        ORDER BY a.starts_at
        """,
        now,
        until,
        STATUS_FAILED,
        STATUS_CLAIMED,
        stale_before,
        STATUS_OPTED_OUT,
    )


async def _claim(pool: asyncpg.Pool, row, *, scheduled_for: datetime, stale_before: datetime) -> int | None:
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
    # Retrying a previous failure, reclaiming a lease whose worker died, or revisiting a
    # reminder skipped while opted out: the WHERE clause is the lock, so only one worker
    # can flip the row and take it.
    return await pool.fetchval(
        """
        UPDATE reminders
        SET status = $2, attempts = attempts + 1, updated_at = now()
        WHERE id = $1
          AND (status = $3 OR (status = $4 AND updated_at < $5) OR status = $6)
        RETURNING id
        """,
        row["reminder_id"],
        STATUS_CLAIMED,
        STATUS_FAILED,
        STATUS_CLAIMED,
        stale_before,
        STATUS_OPTED_OUT,
    )


async def _mark_sending(pool: asyncpg.Pool, reminder_id: int) -> None:
    """Durably record that we are about to hand this message to Telnyx.

    This write is what makes crash recovery safe: once a row is 'sending', a later run
    will not reclaim it, so a message that may already have left is never sent twice.
    """
    await pool.execute(
        "UPDATE reminders SET status = $2, updated_at = now() WHERE id = $1",
        reminder_id,
        STATUS_SENDING,
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
    stale_before = now - CLAIM_LEASE
    today_local = now.astimezone(cfg.tz).date()
    run = ReminderRun()

    for row in await _candidates(pool, now=now, until=until, stale_before=stale_before):
        run.considered += 1

        reminder_id = await _claim(pool, row, scheduled_for=now, stale_before=stale_before)
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

        # Durable "about to send" marker: see _mark_sending. Written before the network
        # call so recovery can tell "never sent" from "maybe sent".
        await _mark_sending(pool, reminder_id)
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
        # Mirror the send into the message log so the patient's inbox thread reads as one
        # conversation. Bookkeeping only -- a failure here must not fail a sent reminder.
        try:
            await store.record_outbound(
                pool, phone=row["phone"], body=body, status="sent", kind="reminder",
                provider_message_id=message_id,
            )
        except Exception:
            log.exception("could not mirror reminder into the message log")
        run.sent += 1

    if run.considered:
        log.info("reminder run: %s", run)
    return run
