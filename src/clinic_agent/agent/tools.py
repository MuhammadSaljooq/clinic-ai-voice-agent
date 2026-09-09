"""Dispatch the agent's tool calls to the scheduling core.

The only module that speaks both the model's vocabulary and the scheduler's API.

Two properties here are security properties, not conveniences:

**Offers are the only bookable thing.** `find_slots` records signed tokens in per-call
state and hands the model plain integers. The model books by option number, so a time
it invented cannot be booked -- the guarantee established in Plan 1 survives all the way
to the phone line, and tokens never enter the prompt.

**Reschedule and cancel are authorised against the caller.** Only appointment ids that
`lookup_appointment` returned *for this caller* may be modified. Without that, a
hallucinated or guessed id could cancel a stranger's appointment.

Known failures are returned as structured results rather than raised, so the agent can
apologise and recover mid-sentence instead of the call falling over.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from clinic_agent.agent.faq import match_faq
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.config import ClinicConfig
from clinic_agent.scheduling.booking import (
    AppointmentNotFound,
    ProviderCannotPerformType,
    SlotTaken,
    book,
    cancel,
    find_upcoming_appointments,
    reschedule,
)
from clinic_agent.scheduling.service import OfferedSlot, available_slots, spoken_datetime
from clinic_agent.scheduling.tokens import InvalidSlotToken

log = logging.getLogger(__name__)

OFFERS_KEY = "offers"
AUTHORISED_KEY = "authorised_appointment_ids"
PATIENT_NAME_KEY = "patient_name"
PATIENT_PHONE_KEY = "patient_phone"
PATIENT_EMAIL_KEY = "patient_email"

TRANSFER_RECOVERY = "Apologise briefly and offer to put the caller through to a person."


@dataclass
class ToolRouter:
    """Stateless dispatcher; all per-call state lives on the ToolContext.

    That means one router can safely serve every concurrent call.
    """

    pool: asyncpg.Pool
    cfg: ClinicConfig
    secret: str
    telnyx: Any | None = None
    # Optional: when absent, bookings simply are not mirrored. The mirror must never
    # be able to affect whether a booking succeeds.
    mirror: Any | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, name: str, args: dict, ctx: ToolContext) -> dict:
        handler = {
            "find_slots": self._find_slots,
            "book_appointment": self._book,
            "lookup_appointment": self._lookup,
            "reschedule_appointment": self._reschedule,
            "cancel_appointment": self._cancel,
            "answer_faq": self._answer_faq,
            "transfer_to_human": self._transfer,
        }.get(name)

        if handler is None:
            return {"error": f"unknown tool {name!r}", "recovery": TRANSFER_RECOVERY}
        return await handler(args or {}, ctx)

    # --- helpers --------------------------------------------------------------

    def _offers(self, ctx: ToolContext) -> dict[int, OfferedSlot]:
        return ctx.state.setdefault(OFFERS_KEY, {})

    def _authorised(self, ctx: ToolContext) -> set[int]:
        return ctx.state.setdefault(AUTHORISED_KEY, set())

    def _type_by_name(self, name: str | None):
        return next((t for t in self.cfg.appointment_types if t.name == name), None)

    def _provider_by_name(self, name: str | None):
        return next((p for p in self.cfg.providers if p.name == name), None)

    async def _mirror(self, event: str, appointment_id: int) -> None:
        """Fire a calendar mirror hook without ever letting it fail the operation.

        By the time these run, the appointment is already committed. Letting an
        exception escape would tell the caller their booking failed when it
        succeeded -- and they would book again, creating a duplicate. CalendarMirror
        swallows its own errors, but this must not depend on that.
        """
        if self.mirror is None:
            return
        try:
            await getattr(self.mirror, f"on_{event}")(self.pool, appointment_id)
        except Exception:
            log.exception("calendar mirror hook on_%s failed for %s", event, appointment_id)

    def _describe(self, appointment) -> dict:
        return {
            "appointment_id": appointment.appointment_id,
            "when": spoken_datetime(appointment.starts_at, self.cfg.tz),
            "provider": appointment.provider_name,
            "type": appointment.appointment_type_name,
        }

    # --- handlers -------------------------------------------------------------

    async def _find_slots(self, args: dict, ctx: ToolContext) -> dict:
        appointment_type = self._type_by_name(args.get("appointment_type"))
        if appointment_type is None:
            return {
                "error": f"unknown appointment type {args.get('appointment_type')!r}",
                "available_types": [t.name for t in self.cfg.appointment_types],
                "recovery": "Ask the caller which of the available types they need.",
            }

        provider_id = None
        if args.get("provider_name"):
            provider = self._provider_by_name(args["provider_name"])
            if provider is None:
                return {
                    "error": f"unknown provider {args['provider_name']!r}",
                    "available_providers": [p.name for p in self.cfg.providers],
                    "recovery": "Ask which provider they meant, or offer anyone available.",
                }
            provider_id = provider.id

        earliest: date | None = None
        if args.get("earliest_date"):
            try:
                earliest = date.fromisoformat(str(args["earliest_date"]))
            except ValueError:
                return {
                    "error": f"earliest_date {args['earliest_date']!r} is not YYYY-MM-DD",
                    "recovery": "Work the date out from the current date and try again.",
                }

        part_of_day = args.get("part_of_day") or None
        try:
            offers = await available_slots(
                self.pool,
                self.cfg,
                self.secret,
                appointment_type_id=appointment_type.id,
                provider_id=provider_id,
                earliest_date=earliest,
                part_of_day=part_of_day,
                now=self.clock(),
                limit=3,
            )
        except ProviderCannotPerformType as exc:
            return {
                "error": str(exc),
                "recovery": "Explain that provider does not offer it, and offer another.",
            }
        except ValueError as exc:
            return {"error": str(exc), "recovery": "Ask the caller to clarify the timing."}

        if not offers:
            return {
                "options": [],
                "note": "Nothing available in that window.",
                "recovery": "Offer to look further ahead, or transfer to a person.",
            }

        numbered = dict(enumerate(offers, start=1))
        ctx.state[OFFERS_KEY] = numbered
        return {
            "options": [
                {
                    "option": number,
                    "when": offer.spoken_time(self.cfg),
                    "provider": offer.provider_name,
                }
                for number, offer in numbered.items()
            ]
        }

    async def _book(self, args: dict, ctx: ToolContext) -> dict:
        offer = self._offers(ctx).get(_as_int(args.get("option")))
        if offer is None:
            return {
                "error": f"option {args.get('option')!r} was not one of the offered times",
                "recovery": "Call find_slots again and read out the fresh options.",
            }

        # First and last name are both required. The model sends first_name/last_name;
        # patient_name is still accepted as a fallback (already-combined name).
        first = (args.get("first_name") or "").strip()
        last = (args.get("last_name") or "").strip()
        if args.get("first_name") is not None or args.get("last_name") is not None:
            if not first or not last:
                return {
                    "error": "both first and last name are required",
                    "recovery": "Ask the caller for their first and last name.",
                }
            patient_name = f"{first} {last}"
        else:
            patient_name = args.get("patient_name") or ctx.state.get(PATIENT_NAME_KEY)
        if not patient_name:
            return {
                "error": "the caller's first and last name are required",
                "recovery": "Ask the caller for their first and last name.",
            }

        # A phone number is required. The model should ask for one (or read back the
        # caller ID to confirm); caller ID and any earlier-given number are fallbacks.
        phone = (
            args.get("phone")
            or args.get("callback_phone")
            or ctx.state.get(PATIENT_PHONE_KEY)
            or ctx.caller_number
        )
        if not phone:
            return {
                "error": "a contact phone number is required",
                "recovery": "Ask the caller for a contact phone number.",
            }

        # Email is an optional second contact method.
        email = (args.get("email") or ctx.state.get(PATIENT_EMAIL_KEY) or "").strip() or None

        try:
            booked = await book(
                self.pool,
                slot_token=offer.token,
                secret=self.secret,
                patient_name=patient_name,
                patient_phone=phone,
                patient_email=email,
                now=self.clock(),
            )
        except SlotTaken:
            ctx.state[OFFERS_KEY] = {}
            return {
                "error": "that time was taken while we were talking",
                "recovery": "Apologise, call find_slots again, and offer the new times.",
            }
        except InvalidSlotToken:
            ctx.state[OFFERS_KEY] = {}
            return {
                "error": "that offer has expired",
                "recovery": "Call find_slots again for current times.",
            }

        ctx.state[PATIENT_NAME_KEY] = patient_name
        ctx.state[PATIENT_PHONE_KEY] = phone
        if email:
            ctx.state[PATIENT_EMAIL_KEY] = email
        # Consume the offers so the same slot cannot be booked twice in one call.
        ctx.state[OFFERS_KEY] = {}
        self._authorised(ctx).add(booked.appointment_id)

        await self._mirror("booked", booked.appointment_id)

        return {
            "booked": True,
            "appointment_id": booked.appointment_id,
            "when": offer.spoken_time(self.cfg),
            "provider": offer.provider_name,
            "reminder": "A text reminder goes out the day before.",
        }

    async def _lookup(self, args: dict, ctx: ToolContext) -> dict:
        phone = args.get("phone") or ctx.caller_number
        if not phone:
            return {
                "error": "no phone number to search",
                "recovery": TRANSFER_RECOVERY,
            }

        found = await find_upcoming_appointments(self.pool, phone=phone, now=self.clock())
        if not found:
            return {
                "appointments": [],
                "note": "No upcoming appointments under that number.",
                "recovery": (
                    "Say you cannot find it under this number and offer to transfer, "
                    "or offer to book a new appointment."
                ),
            }

        self._authorised(ctx).update(a.appointment_id for a in found)
        return {"appointments": [self._describe(a) for a in found]}

    async def _reschedule(self, args: dict, ctx: ToolContext) -> dict:
        appointment_id = _as_int(args.get("appointment_id"))
        if appointment_id is None or appointment_id not in self._authorised(ctx):
            # Authorisation, not validation: without this a guessed id could move a
            # stranger's appointment.
            return {
                "error": "that appointment has not been looked up on this call",
                "recovery": "Call lookup_appointment first.",
            }

        offer = self._offers(ctx).get(_as_int(args.get("option")))
        if offer is None:
            return {
                "error": f"option {args.get('option')!r} was not one of the offered times",
                "recovery": "Call find_slots and offer the new times first.",
            }

        try:
            moved = await reschedule(
                self.pool,
                appointment_id=appointment_id,
                slot_token=offer.token,
                secret=self.secret,
                now=self.clock(),
            )
        except AppointmentNotFound:
            return {
                "error": "that appointment is no longer active",
                "recovery": "Look it up again, or offer to book a new one.",
            }
        except SlotTaken:
            ctx.state[OFFERS_KEY] = {}
            return {
                "error": "that new time was taken while we were talking",
                "recovery": "Apologise, call find_slots again, and offer the new times.",
            }
        except InvalidSlotToken:
            ctx.state[OFFERS_KEY] = {}
            return {"error": "that offer has expired", "recovery": "Call find_slots again."}

        ctx.state[OFFERS_KEY] = {}
        await self._mirror("rescheduled", appointment_id)
        return {
            "rescheduled": True,
            "appointment_id": moved.appointment_id,
            "when": offer.spoken_time(self.cfg),
            "provider": offer.provider_name,
        }

    async def _cancel(self, args: dict, ctx: ToolContext) -> dict:
        appointment_id = _as_int(args.get("appointment_id"))
        if appointment_id is None or appointment_id not in self._authorised(ctx):
            return {
                "error": "that appointment has not been looked up on this call",
                "recovery": "Call lookup_appointment first.",
            }

        try:
            await cancel(self.pool, appointment_id)
        except AppointmentNotFound:
            return {
                "error": "that appointment was already cancelled",
                "recovery": "Confirm to the caller that nothing is booked.",
            }
        await self._mirror("cancelled", appointment_id)
        return {"cancelled": True, "appointment_id": appointment_id}

    async def _answer_faq(self, args: dict, ctx: ToolContext) -> dict:
        entry = match_faq(str(args.get("question", "")), self.cfg.faq)
        if entry is None:
            return {
                "error": "no answer on file for that question",
                "recovery": (
                    "Do not guess. Say you will put them through to someone who knows, "
                    "and transfer."
                ),
            }
        return {"answer": entry.a}

    async def _transfer(self, args: dict, ctx: ToolContext) -> dict:
        destination = self.cfg.clinic.human_transfer_number
        if self.telnyx is None or not ctx.call_control_id:
            return {
                "error": "cannot transfer right now",
                "recovery": f"Give the caller the number to call directly: {destination}.",
            }
        await self.telnyx.transfer(ctx.call_control_id, to=destination)
        return {
            "transferring": True,
            "reason": args.get("reason", ""),
            "say": "Let them know you're putting them through now.",
        }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
