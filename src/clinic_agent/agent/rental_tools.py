"""Dispatch the trailer-rental agent's tool calls to the rentals domain.

Mirrors `agent/tools.py` and keeps its two security properties: the model can only book a
signed option it was offered on this call (no invented availability/prices), and it can
only cancel a rental that `lookup_rental` returned on this call.
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
from clinic_agent.rental_config import RentalConfig
from clinic_agent.rentals.availability import available_options
from clinic_agent.rentals.booking import (
    RentalNotFound,
    UnitTaken,
    book,
    cancel,
    find_upcoming_rentals,
)
from clinic_agent.rentals.models import RentalOption
from clinic_agent.rentals.tokens import InvalidRentalToken

log = logging.getLogger(__name__)

OFFERS_KEY = "rental_offers"
AUTHORISED_KEY = "authorised_rental_ids"
CUSTOMER_NAME_KEY = "customer_name"
CUSTOMER_PHONE_KEY = "customer_phone"
CUSTOMER_EMAIL_KEY = "customer_email"

TRANSFER_RECOVERY = "Apologise briefly and offer to put the caller through to a person."


@dataclass
class RentalToolRouter:
    pool: asyncpg.Pool
    cfg: RentalConfig
    secret: str
    telnyx: Any | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, name: str, args: dict, ctx: ToolContext) -> dict:
        handler = {
            "find_available_trailers": self._find,
            "book_rental": self._book,
            "lookup_rental": self._lookup,
            "cancel_rental": self._cancel,
            "answer_faq": self._answer_faq,
            "transfer_to_human": self._transfer,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}", "recovery": TRANSFER_RECOVERY}
        return await handler(args or {}, ctx)

    def _offers(self, ctx: ToolContext) -> dict[int, RentalOption]:
        return ctx.state.setdefault(OFFERS_KEY, {})

    def _authorised(self, ctx: ToolContext) -> set[int]:
        return ctx.state.setdefault(AUTHORISED_KEY, set())

    async def _find(self, args: dict, ctx: ToolContext) -> dict:
        type_id = None
        if args.get("trailer_type"):
            t = self.cfg.type_by_name(args["trailer_type"])
            if t is None:
                return {
                    "error": f"unknown trailer type {args['trailer_type']!r}",
                    "available_types": [x.name for x in self.cfg.trailer_types],
                    "recovery": "Ask which of the available trailers they mean.",
                }
            type_id = t.id

        try:
            pickup = date.fromisoformat(str(args.get("pickup_date")))
            return_ = date.fromisoformat(str(args.get("return_date")))
        except (TypeError, ValueError):
            return {
                "error": "pickup_date and return_date must be YYYY-MM-DD",
                "recovery": "Work the dates out from the current date and try again.",
            }

        try:
            options = await available_options(
                self.pool, self.cfg, self.secret,
                type_id=type_id, pickup=pickup, return_=return_, now=self.clock(),
            )
        except ValueError as exc:
            return {"error": str(exc), "recovery": "Explain that plainly and ask for workable dates."}

        if not options:
            return {
                "options": [],
                "note": "No trailers free for those dates.",
                "recovery": "Offer another trailer type or different dates, or transfer.",
            }

        numbered = dict(enumerate(options, start=1))
        ctx.state[OFFERS_KEY] = numbered
        return {
            "options": [
                {
                    "option": n,
                    "trailer": o.type_name,
                    "pickup": o.pickup.isoformat(),
                    "return": o.return_.isoformat(),
                    "days": o.days,
                    "total": f"{o.total:.2f}",
                    "deposit": f"{o.deposit:.2f}",
                }
                for n, o in numbered.items()
            ]
        }

    async def _book(self, args: dict, ctx: ToolContext) -> dict:
        offer = self._offers(ctx).get(_as_int(args.get("option")))
        if offer is None:
            return {
                "error": f"option {args.get('option')!r} was not one of the offered trailers",
                "recovery": "Call find_available_trailers again and read out the options.",
            }

        first = (args.get("first_name") or "").strip()
        last = (args.get("last_name") or "").strip()
        if not first or not last:
            return {
                "error": "both first and last name are required",
                "recovery": "Ask the caller for their first and last name.",
            }
        name = f"{first} {last}"

        phone = (args.get("phone") or ctx.state.get(CUSTOMER_PHONE_KEY) or ctx.caller_number)
        if not phone:
            return {
                "error": "a contact phone number is required",
                "recovery": "Ask the caller for a contact phone number.",
            }
        email = (args.get("email") or ctx.state.get(CUSTOMER_EMAIL_KEY) or "").strip() or None

        try:
            booked = await book(
                self.pool, rental_token=offer.token, secret=self.secret,
                customer_name=name, customer_phone=phone, customer_email=email, now=self.clock(),
            )
        except UnitTaken:
            ctx.state[OFFERS_KEY] = {}
            return {
                "error": "that trailer was taken while we were talking",
                "recovery": "Apologise, call find_available_trailers again, and offer what's left.",
            }
        except InvalidRentalToken:
            ctx.state[OFFERS_KEY] = {}
            return {"error": "that quote has expired", "recovery": "Check availability again."}
        except ValueError as exc:
            return {"error": str(exc), "recovery": "Check availability again for a current quote."}

        ctx.state[CUSTOMER_NAME_KEY] = name
        ctx.state[CUSTOMER_PHONE_KEY] = phone
        if email:
            ctx.state[CUSTOMER_EMAIL_KEY] = email
        ctx.state[OFFERS_KEY] = {}
        self._authorised(ctx).add(booked.rental_id)

        return {
            "booked": True,
            "rental_id": booked.rental_id,
            "trailer": offer.type_name,
            "pickup": booked.pickup.isoformat(),
            "return": booked.return_.isoformat(),
            "total": f"{booked.total:.2f}",
            "deposit": f"{booked.deposit:.2f}",
            "reminder": "Bring a valid driver's license and the right hitch and ball at pickup.",
        }

    async def _lookup(self, args: dict, ctx: ToolContext) -> dict:
        phone = args.get("phone") or ctx.caller_number
        if not phone:
            return {"error": "no phone number to search", "recovery": TRANSFER_RECOVERY}
        found = await find_upcoming_rentals(self.pool, phone=phone, now=self.clock())
        if not found:
            return {
                "rentals": [],
                "note": "No upcoming rentals under that number.",
                "recovery": "Say you can't find it under this number and offer to transfer.",
            }
        self._authorised(ctx).update(r.rental_id for r in found)
        return {
            "rentals": [
                {
                    "rental_id": r.rental_id,
                    "trailer": r.type_name,
                    "pickup": r.pickup.isoformat(),
                    "return": r.return_.isoformat(),
                    "total": f"{r.total:.2f}",
                }
                for r in found
            ]
        }

    async def _cancel(self, args: dict, ctx: ToolContext) -> dict:
        rental_id = _as_int(args.get("rental_id"))
        if rental_id is None or rental_id not in self._authorised(ctx):
            return {
                "error": "that rental has not been looked up on this call",
                "recovery": "Call lookup_rental first.",
            }
        try:
            await cancel(self.pool, rental_id)
        except RentalNotFound:
            return {
                "error": "that rental was already cancelled",
                "recovery": "Confirm to the caller that nothing is booked.",
            }
        return {"cancelled": True, "rental_id": rental_id}

    async def _answer_faq(self, args: dict, ctx: ToolContext) -> dict:
        entry = match_faq(str(args.get("question", "")), self.cfg.faq)
        if entry is None:
            return {
                "error": "no answer on file for that question",
                "recovery": "Do not guess. Offer to put them through to someone who knows.",
            }
        return {"answer": entry.a}

    async def _transfer(self, args: dict, ctx: ToolContext) -> dict:
        destination = self.cfg.business.human_transfer_number
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
