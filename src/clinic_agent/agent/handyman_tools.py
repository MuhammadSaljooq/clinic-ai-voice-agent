"""Dispatch the handyman agent's tool calls to Postgres.

Mirrors `agent/tools.py` and `agent/rental_tools.py`, but this agent has no calendar or
pricing -- it records requests, leads, and messages for the owner to action. Inserts are
inline (the clinic `_request_callback` pattern). Known failures come back as structured
results so the agent can recover mid-sentence rather than the call falling over.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from clinic_agent.agent.faq import match_faq
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.handyman_config import HandymanConfig

log = logging.getLogger(__name__)

NAME_KEY = "caller_name"
PHONE_KEY = "caller_phone"
EMAIL_KEY = "caller_email"

# Deliberately permissive: one @, a dot in the domain, no spaces. Enough to catch a
# mis-heard or half-given address without rejecting unusual-but-valid ones.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TRANSFER_RECOVERY = "Apologise briefly and offer to put the caller through to a person."


@dataclass
class HandymanToolRouter:
    """Stateless dispatcher; all per-call state lives on the ToolContext, so one router
    safely serves every concurrent call."""

    pool: asyncpg.Pool
    cfg: HandymanConfig
    telnyx: Any | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, name: str, args: dict, ctx: ToolContext) -> dict:
        handler = {
            "answer_question": self._answer_question,
            "request_appointment": self._request_appointment,
            "capture_lead": self._capture_lead,
            "take_message": self._take_message,
            "transfer_to_human": self._transfer,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}", "recovery": TRANSFER_RECOVERY}
        return await handler(args or {}, ctx)

    def _name(self, args: dict, ctx: ToolContext) -> str | None:
        return (args.get("name") or "").strip() or ctx.state.get(NAME_KEY) or None

    def _phone(self, args: dict, ctx: ToolContext) -> str | None:
        return (args.get("phone") or "").strip() or ctx.state.get(PHONE_KEY) or ctx.caller_number

    def _email(self, args: dict, ctx: ToolContext) -> str | None:
        return (args.get("email") or "").strip() or ctx.state.get(EMAIL_KEY) or None

    def _remember(self, ctx: ToolContext, name: str | None, phone: str | None,
                  email: str | None = None) -> None:
        if name:
            ctx.state[NAME_KEY] = name
        if phone:
            ctx.state[PHONE_KEY] = phone
        if email:
            ctx.state[EMAIL_KEY] = email

    async def _answer_question(self, args: dict, ctx: ToolContext) -> dict:
        entry = match_faq(str(args.get("question", "")), self.cfg.faq)
        if entry is None:
            return {
                "error": "no answer on file for that question",
                "recovery": (
                    "Do not guess or quote a price. Say you'll check with the owner and offer "
                    "to take their details for a free estimate or a callback."
                ),
            }
        return {"answer": entry.a}

    async def _request_appointment(self, args: dict, ctx: ToolContext) -> dict:
        # A proper booking needs a name, a phone, and a valid email. Missing pieces come
        # back as a structured recovery so the agent asks for exactly what's left.
        name = self._name(args, ctx)
        phone = self._phone(args, ctx)
        email = self._email(args, ctx)

        if not name:
            return {
                "error": "the caller's name is required",
                "recovery": "Ask the caller for their full name.",
            }
        if not phone:
            return {
                "error": "a contact phone number is required",
                "recovery": "Ask the caller for the best number to reach them on, and read it back.",
            }
        if not email:
            return {
                "error": "an email address is required for the booking",
                "recovery": (
                    "Ask for their email so the owner can send the estimate. If they truly "
                    "don't have one, offer to take it as a callback lead instead with capture_lead."
                ),
            }
        if not _EMAIL_RE.match(email):
            return {
                "error": f"the email {email!r} does not look valid",
                "recovery": "Read the email back and ask them to confirm or correct it.",
            }

        job_type = (args.get("job_type") or "").strip() or None
        description = (args.get("description") or "").strip() or None
        address = (args.get("address") or "").strip() or None
        preferred_time = (args.get("preferred_time") or "").strip() or None

        await self.pool.execute(
            """
            INSERT INTO handyman_appointment_requests
                (name, phone, email, job_type, description, address, preferred_time)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            name, phone, email, job_type, description, address, preferred_time,
        )
        self._remember(ctx, name, phone, email)
        return {
            "requested": True,
            "say": (
                "Confirm warmly that you've noted the request and the owner will reach out to "
                "confirm the time and give a free estimate. Do NOT say it is booked."
            ),
        }

    async def _capture_lead(self, args: dict, ctx: ToolContext) -> dict:
        name = self._name(args, ctx)
        phone = self._phone(args, ctx)
        if not phone:
            return {
                "error": "a phone number is required for a callback",
                "recovery": "Ask the caller for a number to call them back on.",
            }
        reason = (args.get("reason") or "").strip() or None
        await self.pool.execute(
            "INSERT INTO handyman_leads (name, phone, reason) VALUES ($1, $2, $3)",
            name, phone, reason,
        )
        self._remember(ctx, name, phone)
        return {
            "queued": True,
            "say": "Confirm the owner will call them back as soon as he can.",
        }

    async def _take_message(self, args: dict, ctx: ToolContext) -> dict:
        message = (args.get("message") or "").strip()
        if not message:
            return {
                "error": "there is no message to take",
                "recovery": "Ask the caller what they'd like you to pass along.",
            }
        name = (args.get("caller_name") or "").strip() or ctx.state.get(NAME_KEY) or None
        phone = (args.get("phone") or "").strip() or ctx.state.get(PHONE_KEY) or ctx.caller_number
        await self.pool.execute(
            "INSERT INTO handyman_messages (caller_name, phone, message) VALUES ($1, $2, $3)",
            name, phone, message,
        )
        self._remember(ctx, name, phone)
        return {
            "taken": True,
            "say": "Confirm you'll pass the message along to the owner.",
        }

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
