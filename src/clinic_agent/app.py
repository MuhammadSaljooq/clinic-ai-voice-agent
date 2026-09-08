"""FastAPI surface: the Telnyx webhook and the media-streaming WebSocket.

Dependencies are injected rather than read from the environment inside handlers, so
the whole app is testable without credentials, without a Telnyx account, and without
touching the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

from clinic_agent.bridge.call_session import CallSession, ToolHandler
from clinic_agent.config import ClinicConfig
from clinic_agent.telephony.signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    InvalidWebhookSignature,
    verify_webhook,
)
from clinic_agent.telephony.telnyx_client import TelnyxClient

log = logging.getLogger(__name__)


@dataclass
class AppDeps:
    cfg: ClinicConfig
    telnyx: TelnyxClient
    public_key_b64: str
    stream_url: str
    connect_gemini: Callable[[str | None], Any]
    tool_handler: ToolHandler | None = None
    on_call_finished: Callable[[Any], Any] | None = None


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


def create_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(title="Clinic AI Voice Agent")

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

    @app.websocket("/telnyx/stream")
    async def telnyx_stream(websocket: WebSocket) -> None:
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
                "call %s ended (%s) after %d reconnect(s)",
                outcome.call_control_id,
                outcome.ended_reason,
                outcome.reconnects,
            )
            if deps.on_call_finished is not None:
                await deps.on_call_finished(outcome)

    return app
