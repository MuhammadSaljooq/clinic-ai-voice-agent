"""FastAPI surface: the Telnyx webhook and the media-streaming WebSocket.

Dependencies are injected rather than read from the environment inside handlers, so
the whole app is testable without credentials, without a Telnyx account, and without
touching the network.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

from clinic_agent.bridge.call_session import CallSession, ToolHandler
from clinic_agent.config import ClinicConfig
from clinic_agent.messaging import store
from clinic_agent.messaging.inbound import handle_inbound
from clinic_agent.messaging.outbound import DELIVERY_STATUS
from clinic_agent.telephony.signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    InvalidWebhookSignature,
    verify_webhook,
)
from clinic_agent.telephony.telnyx_client import TelnyxClient
from clinic_agent.web.auth import build_auth_router
from clinic_agent.web.dashboard import build_dashboard

log = logging.getLogger(__name__)

# Telnyx messaging events that report what happened to a message we sent, rather than a
# new inbound message. Handled so the inbox can show delivered / failed, not just "sent".
DELIVERY_EVENTS = frozenset({"message.sent", "message.finalized"})


async def _record_inbound_message(pool: Any, *, phone: str, text: str) -> None:
    """Persist a received SMS for the inbox. Bookkeeping only -- never fail the webhook
    over it, since the opt-out/cancel state change already happened and matters more."""
    if pool is None or not phone or not text:
        return
    try:
        await store.record_inbound(pool, phone=phone, body=text)
    except Exception:
        log.exception("could not persist an inbound SMS")


async def _record_auto_reply(pool: Any, *, phone: str, text: str, sent: bool, error: str | None) -> None:
    if pool is None or not phone or not text:
        return
    try:
        await store.record_outbound(
            pool, phone=phone, body=text, kind="auto_reply",
            status="sent" if sent else "failed", error=error,
        )
    except Exception:
        log.exception("could not persist an auto-reply SMS")


async def _record_delivery(pool: Any, message: dict) -> None:
    """Update a sent message from a Telnyx delivery receipt."""
    if pool is None:
        return
    provider_id = message.get("id")
    recipients = message.get("to") or []
    raw_status = recipients[0].get("status") if recipients else None
    mapped = DELIVERY_STATUS.get(str(raw_status))
    if not provider_id or not mapped:
        return
    error = raw_status if mapped == "failed" else None
    try:
        await store.mark_delivery(pool, provider_message_id=str(provider_id), status=mapped, error=error)
    except Exception:
        log.exception("could not apply a delivery receipt")


@dataclass
class AppDeps:
    cfg: ClinicConfig
    telnyx: TelnyxClient
    public_key_b64: str
    stream_url: str
    connect_gemini: Callable[[str | None], Any]
    tool_handler: ToolHandler | None = None
    on_call_finished: Callable[[Any], Any] | None = None
    # Needed only by the messaging webhook; the voice path does not touch the DB
    # directly, it goes through the tool handler.
    pool: Any | None = None
    sms_from_number: str | None = None
    mirror: Any | None = None
    # Dashboard is mounted only when a password is set, so it cannot be exposed by
    # forgetting to configure it.
    dashboard_password: str | None = None
    # When set, the media-streaming WebSocket requires this secret in its URL, so a
    # stranger who finds the endpoint cannot open a Gemini-billed session or spoof a
    # caller. Telnyx frames are unsigned, so the URL secret is the lever we have.
    stream_secret: str | None = None


class TelnyxWebSocketAdapter:
    """Presents a FastAPI WebSocket with the interface CallSession expects."""

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def send(self, text: str) -> None:
        await self._ws.send_text(text)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self._ws.receive_text()
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise StopAsyncIteration from exc


def create_app(deps: AppDeps, *, lifespan: Any | None = None) -> FastAPI:
    # `deps.tool_handler` is read when a call arrives, not when the app is built,
    # so a lifespan can fill it in once the database pool exists.
    app = FastAPI(title="Clinic AI Voice Agent", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "clinic": deps.cfg.clinic.name}

    @app.post("/telnyx/webhook")
    async def telnyx_webhook(request: Request) -> Response:
        body = await request.body()
        try:
            verify_webhook(
                body=body,
                signature_b64=request.headers.get(SIGNATURE_HEADER, ""),
                timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
                public_key_b64=deps.public_key_b64,
            )
        except InvalidWebhookSignature as exc:
            # An unverified webhook is an open control channel for the clinic's phone
            # line, so refuse before looking at the payload at all.
            log.warning("rejected Telnyx webhook: %s", exc)
            return Response(status_code=401, content="invalid signature")

        payload = await request.json()
        data = payload.get("data") or {}
        event = data.get("event_type")
        call_control_id = (data.get("payload") or {}).get("call_control_id")

        if event == "call.initiated" and call_control_id:
            await deps.telnyx.answer_with_stream(
                call_control_id, stream_url=deps.stream_url
            )
            return Response(status_code=200, content="answered")

        log.debug("ignoring Telnyx event %r", event)
        return Response(status_code=200, content="ignored")

    @app.post("/telnyx/messaging")
    async def telnyx_messaging(request: Request) -> Response:
        """Inbound SMS: STOP, START, HELP, and C to cancel.

        Opt-out is a legal requirement, so this endpoint has to work before any
        reminder is ever sent.
        """
        body = await request.body()
        try:
            verify_webhook(
                body=body,
                signature_b64=request.headers.get(SIGNATURE_HEADER, ""),
                timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
                public_key_b64=deps.public_key_b64,
            )
        except InvalidWebhookSignature as exc:
            log.warning("rejected Telnyx messaging webhook: %s", exc)
            return Response(status_code=401, content="invalid signature")

        payload = await request.json()
        data = payload.get("data") or {}
        event = data.get("event_type")
        message = data.get("payload") or {}

        # A delivery receipt for a message we sent: update the inbox, then done.
        if event in DELIVERY_EVENTS:
            await _record_delivery(deps.pool, message)
            return Response(status_code=200, content="delivery")

        if event != "message.received":
            return Response(status_code=200, content="ignored")

        # Checked after the event filter: ignoring an irrelevant event needs no database.
        if deps.pool is None:
            log.error("messaging webhook received but no database pool is configured")
            return Response(status_code=200, content="not configured")

        sender = (message.get("from") or {}).get("phone_number") or ""
        # Message text is written by whoever sent it: data, never instructions.
        text = message.get("text") or ""

        # Persist the inbound message first, so it appears in the inbox even if the
        # keyword handling below decides there is nothing to reply.
        await _record_inbound_message(deps.pool, phone=sender, text=text)

        result = await handle_inbound(
            deps.pool, deps.cfg, from_number=sender, text=text, mirror=deps.mirror
        )
        log.info("inbound SMS handled: %s", result.action)

        if result.reply and sender and deps.sms_from_number:
            reply_error: str | None = None
            try:
                await deps.telnyx.send_sms(
                    to=sender, from_=deps.sms_from_number, text=result.reply
                )
            except Exception as exc:
                # The state change already happened and matters more than the reply.
                log.exception("could not send the SMS reply")
                reply_error = str(exc)[:500]
            await _record_auto_reply(
                deps.pool, phone=sender, text=result.reply,
                sent=reply_error is None, error=reply_error,
            )

        return Response(status_code=200, content=result.action)

    if deps.dashboard_password:
        # pool, telnyx and the sender number are resolved lazily: the lifespan fills
        # them in after the app is built. The login page and session live at the app
        # root; the console under /dashboard.
        app.include_router(build_auth_router(deps.cfg, deps.dashboard_password))
        app.include_router(
            build_dashboard(
                deps.cfg,
                lambda: deps.pool,
                deps.dashboard_password,
                telnyx=deps.telnyx,
                from_number_getter=lambda: deps.sms_from_number,
                connect_gemini=deps.connect_gemini,
                tool_handler_getter=lambda: deps.tool_handler,
            )
        )

    def _stream_authorised(token: str | None) -> bool:
        # No secret configured: accept anything (dev / backward compatible), but the
        # caller is warned at startup that the socket is open.
        if not deps.stream_secret:
            return True
        return bool(token) and secrets.compare_digest(token, deps.stream_secret)

    async def _run_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        session = CallSession(
            telnyx=TelnyxWebSocketAdapter(websocket),
            connect_gemini=deps.connect_gemini,
            tool_handler=deps.tool_handler,
        )
        try:
            outcome = await session.run()
        except Exception:
            log.exception("call session failed")
            raise
        else:
            log.info(
                "call %s ended (%s) after %d reconnect(s), %d transcript line(s)",
                outcome.call_control_id,
                outcome.ended_reason,
                outcome.reconnects,
                len(outcome.transcript),
            )
            # The transcript is the fastest way to see what actually happened on a
            # call, so it goes in the log rather than only into the outcome object.
            for entry in outcome.transcript:
                log.info("  %-6s %s", entry["role"], entry["text"])
            if deps.on_call_finished is not None:
                await deps.on_call_finished(outcome)

    @app.websocket("/telnyx/stream")
    async def telnyx_stream(websocket: WebSocket) -> None:
        # The secret may also arrive as a query param (?token=...), for hosts that
        # prefer not to put it in the path.
        if not _stream_authorised(websocket.query_params.get("token")):
            log.warning("rejected an unauthorised /telnyx/stream connection")
            await websocket.close(code=1008)  # policy violation
            return
        await _run_stream(websocket)

    @app.websocket("/telnyx/stream/{token}")
    async def telnyx_stream_token(websocket: WebSocket, token: str) -> None:
        if not _stream_authorised(token):
            log.warning("rejected an unauthorised /telnyx/stream connection")
            await websocket.close(code=1008)
            return
        await _run_stream(websocket)

    return app
