"""Handle inbound SMS: opt-out, opt-in, help, and cancelling by reply.

Opt-out is a legal requirement, not a nicety, and the reminder copy promises both
`STOP` and `C` -- so both have to actually work.

**A real conflict worth naming.** Carriers treat the word `CANCEL` as a standard
opt-out keyword, alongside STOP/END/QUIT/UNSUBSCRIBE. But a patient replying "cancel"
almost certainly means *cancel my appointment*, not *stop texting me*. Honouring
compliance wins -- `CANCEL` opts them out -- so the reminder deliberately asks for the
single letter `C` instead, and the opt-out reply explains how to cancel an appointment
so nobody is left stuck.

Message text is written by whoever sent it: treated purely as data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from clinic_agent.config import ClinicConfig
from clinic_agent.reminders.messages import (
    compose_cancellation_confirmation,
    compose_optin_confirmation,
    compose_optout_confirmation,
)
from clinic_agent.scheduling.booking import cancel, find_upcoming_appointments

log = logging.getLogger(__name__)

# Carrier-standard opt-out keywords. CANCEL is included because the standard requires
# it, even though it collides with cancelling an appointment -- see the module note.
OPT_OUT_WORDS = frozenset({
    "STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT", "CANCEL", "REVOKE", "OPTOUT",
})
OPT_IN_WORDS = frozenset({"START", "YES", "UNSTOP", "OPTIN"})
HELP_WORDS = frozenset({"HELP", "INFO"})
CANCEL_APPOINTMENT_WORDS = frozenset({"C"})


@dataclass(frozen=True, slots=True)
class InboundResult:
    action: str
    reply: str | None = None


def normalise(text: str) -> str:
    """Callers do not type carefully: 'stop.', ' Stop ', 'STOP!' must all work."""
    return re.sub(r"[^A-Z ]", "", (text or "").upper()).strip()


async def _set_opt_out(pool: asyncpg.Pool, phone: str, opted_out: bool) -> None:
    # Upsert rather than update: an opt-out from a number we have never seen must
    # still be honoured, or a later booking under that number could text them.
    await pool.execute(
        """
        INSERT INTO patients (name, phone, sms_opted_out) VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE SET sms_opted_out = EXCLUDED.sms_opted_out
        """,
        "(unknown, opted out by SMS)" if opted_out else "(unknown)",
        phone,
        opted_out,
    )


async def handle_inbound(
    pool: asyncpg.Pool,
    cfg: ClinicConfig,
    *,
    from_number: str,
    text: str,
    now: datetime | None = None,
) -> InboundResult:
    now = now or datetime.now(UTC)
    word = normalise(text)
    clinic = cfg.clinic.name
    phone_display = cfg.clinic.phone_display

    if not from_number:
        return InboundResult("ignored")

    if word in OPT_OUT_WORDS:
        await _set_opt_out(pool, from_number, True)
        log.info("opt-out recorded for a patient number")
        reply = compose_optout_confirmation(clinic)
        if word == "CANCEL":
            # Do not leave them thinking their appointment is cancelled.
            reply += f" To cancel an appointment, reply C or call {phone_display}."
        return InboundResult("opted_out", reply)

    if word in OPT_IN_WORDS:
        await _set_opt_out(pool, from_number, False)
        return InboundResult("opted_in", compose_optin_confirmation(clinic))

    if word in HELP_WORDS:
        return InboundResult(
            "help",
            f"{clinic}: reply C to cancel your next appointment, "
            f"STOP to opt out, or call {phone_display}.",
        )

    if word in CANCEL_APPOINTMENT_WORDS:
        upcoming = await find_upcoming_appointments(pool, phone=from_number, now=now)
        if not upcoming:
            return InboundResult(
                "no_appointment",
                f"{clinic}: we could not find an upcoming appointment for this number. "
                f"Please call {phone_display}.",
            )
        # Cancel only the soonest, and say which. Cancelling everything off a
        # one-letter text would be too blunt.
        soonest = upcoming[0]
        await cancel(pool, soonest.appointment_id)
        log.info("appointment %s cancelled by SMS", soonest.appointment_id)
        return InboundResult(
            "cancelled", compose_cancellation_confirmation(clinic, phone_display)
        )

    # Anything else: do not guess, do not change state.
    return InboundResult(
        "unrecognised",
        f"{clinic}: sorry, we can only handle C to cancel, STOP to opt out, or HELP. "
        f"Please call {phone_display}.",
    )
