"""Bridge one Telnyx phone call to one Gemini Live session.

Three cooperating tasks:

* **reader** -- pulls Telnyx frames, decodes them, queues inbound audio
* **writer** -- drains outbound audio to Telnyx as paced 20 ms frames
* **gemini loop** -- owns the Live session, reconnecting across the ~10-minute reset

Inbound audio lands in a queue rather than going straight to the model, so audio that
arrives while we are reconnecting is buffered instead of dropped. The queue survives
a reconnect; the session does not.

Tool calls are awaited inline in the receive loop. That is deliberate: every tool hits
local Postgres in about a millisecond, and the audio writer is a separate task so
speech keeps flowing regardless. If a tool ever becomes genuinely slow, move dispatch
into its own task -- but inline keeps ordering and error handling obvious.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from clinic_agent.audio.codec import frame_20ms, gemini_to_telnyx, telnyx_to_gemini
from clinic_agent.telephony.stream_protocol import (
    Dtmf,
    ErrorFrame,
    MalformedFrame,
    MediaIn,
    StreamStart,
    StreamStop,
    UnknownEvent,
    decode_frame,
    encode_clear,
    encode_media,
)

log = logging.getLogger(__name__)

SUPPORTED_ENCODING = "L16"
FRAME_SECONDS = 0.02
GEMINI_INPUT_MIME = "audio/pcm;rate=16000"
TRANSFER_TOOL = "transfer_to_human"


@dataclass(slots=True)
class ToolContext:
    """What a tool handler knows about the call it is serving."""

    caller_number: str
    call_control_id: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallOutcome:
    call_control_id: str = ""
    from_number: str = ""
    transcript: list[dict[str, str]] = field(default_factory=list)
    reconnects: int = 0
    ended_reason: str = "telnyx_closed"


ToolHandler = Callable[[str, dict, ToolContext], Awaitable[dict]]
GeminiConnector = Callable[[str | None], Any]  # (handle) -> async context manager


class CallSession:
    def __init__(
        self,
        *,
        telnyx: Any,
        connect_gemini: GeminiConnector,
        tool_handler: ToolHandler | None = None,
        max_reconnects: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._telnyx = telnyx
        self._connect_gemini = connect_gemini
        self._tool_handler = tool_handler
        self._max_reconnects = max_reconnects
        self._sleep = sleep

        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._started = asyncio.Event()
        self._call_ended = asyncio.Event()

        self.resumption_handle: str | None = None
        self._context: ToolContext | None = None
        self._outcome = CallOutcome()

        self._saw_stop = False
        self._unsupported_codec = False
        self._hit_reconnect_limit = False

    # --- public ---------------------------------------------------------------

    async def run(self) -> CallOutcome:
        reader = asyncio.create_task(self._read_telnyx(), name="telnyx-reader")
        gemini = asyncio.create_task(self._gemini_loop(), name="gemini-loop")
        writer = asyncio.create_task(self._write_telnyx(), name="telnyx-writer")

        try:
            done, _pending = await asyncio.wait(
                {reader, gemini}, return_when=asyncio.FIRST_COMPLETED
            )

            if reader in done:
                # The socket closed, so the call is over. Let the Gemini loop finish
                # the session it is serving -- otherwise queued audio and any tool
                # response still in flight are thrown away.
                self._call_ended.set()
                await gemini
            else:
                # No Gemini session means there is no agent, so end the call.
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

            for task in (reader, gemini):
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
        finally:
            self._call_ended.set()
            # Let already-generated speech reach Telnyx before we tear the writer down.
            await self._flush_outbound()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

        self._outcome.ended_reason = self._resolve_ended_reason()
        return self._outcome

    async def _flush_outbound(self, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._outbound.empty() and loop.time() < deadline:
            await asyncio.sleep(0.001)

    # --- Telnyx side ----------------------------------------------------------

    async def _read_telnyx(self) -> None:
        async for raw in self._telnyx:
            try:
                frame = decode_frame(raw)
            except MalformedFrame as exc:
                # A single bad frame must never drop a live call.
                log.warning("discarding malformed Telnyx frame: %s", exc)
                continue

            if isinstance(frame, StreamStart):
                self._on_start(frame)
                if self._unsupported_codec:
                    break
                continue

            if isinstance(frame, MediaIn):
                # Media before `start` cannot be interpreted: we do not yet know the
                # negotiated format.
                if self._started.is_set() and frame.audio:
                    self._inbound.put_nowait(frame.audio)
                continue

            if isinstance(frame, StreamStop):
                self._saw_stop = True
                break

            if isinstance(frame, Dtmf):
                await self._on_dtmf(frame.digit)
                continue

            if isinstance(frame, ErrorFrame):
                log.warning("Telnyx stream error %s: %s %s", frame.code, frame.title, frame.detail)
                continue

            if isinstance(frame, UnknownEvent):
                log.info("ignoring unrecognised Telnyx event %r", frame.event)

    def _on_start(self, frame: StreamStart) -> None:
        self._outcome.call_control_id = frame.call_control_id
        self._outcome.from_number = frame.from_number
        self._context = ToolContext(
            caller_number=frame.from_number, call_control_id=frame.call_control_id
        )

        if frame.encoding and frame.encoding.upper() != SUPPORTED_ENCODING:
            # Playing another codec as if it were L16 emits loud noise at the caller.
            # Refusing is the kinder failure.
            log.error("unsupported stream encoding %r; expected %s", frame.encoding, SUPPORTED_ENCODING)
            self._unsupported_codec = True
            self._call_ended.set()
            return

        self._started.set()

    async def _on_dtmf(self, digit: str) -> None:
        """Handled outside the model, so the escape hatch always works."""
        if digit == "0":
            await self._invoke_tool_safely(
                TRANSFER_TOOL, {"reason": "caller pressed 0"}
            )

    async def _write_telnyx(self) -> None:
        while True:
            frame = await self._outbound.get()
            await self._telnyx.send(encode_media(frame))
            # Paced rather than flushed, so we never trip Telnyx frame rate limits.
            await self._sleep(FRAME_SECONDS)

    async def _handle_interruption(self) -> None:
        """Caller started talking: stop immediately, everywhere.

        Dropping our own queue is not enough. Telnyx has already buffered audio it has
        not played, and without `clear` the caller keeps hearing the agent for several
        hundred milliseconds after interrupting -- the single most robot-like thing a
        voice agent does.
        """
        dropped = 0
        while not self._outbound.empty():
            self._outbound.get_nowait()
            dropped += 1
        log.debug("interrupted: dropped %d queued frames", dropped)
        await self._telnyx.send(encode_clear())

    # --- Gemini side ----------------------------------------------------------

    async def _gemini_loop(self) -> None:
        # Nothing to connect until we know the stream is usable.
        started = asyncio.create_task(self._started.wait())
        ended = asyncio.create_task(self._call_ended.wait())
        await asyncio.wait({started, ended}, return_when=asyncio.FIRST_COMPLETED)
        for task in (started, ended):
            task.cancel()
        if not self._started.is_set():
            return

        attempts = 0
        while True:
            reconnect_requested = False
            try:
                async with self._connect_gemini(self.resumption_handle) as session:
                    reconnect_requested = await self._serve_session(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Gemini session failed; will retry if budget remains")
                reconnect_requested = True

            if self._call_ended.is_set():
                return

            attempts += 1
            if attempts > self._max_reconnects:
                self._hit_reconnect_limit = True
                return

            self._outcome.reconnects += 1
            log.info(
                "reconnecting Gemini session (attempt %d, handle=%s, requested=%s)",
                attempts,
                self.resumption_handle,
                reconnect_requested,
            )

    async def _serve_session(self, session: Any) -> bool:
        """Pump audio and events for one Live session. Returns True if it asked us to go."""
        pump = asyncio.create_task(self._pump_inbound(session), name="gemini-inbound")
        go_away = False
        try:
            async for message in session.receive():
                if message.go_away is not None:
                    # Google warns before resetting the socket, so we can reconnect
                    # on our own terms rather than mid-sentence.
                    log.info("Gemini go_away received; reconnecting")
                    go_away = True
                    break
                await self._on_gemini_message(session, message)
        finally:
            if not go_away:
                await self._drain_inbound(session)
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        return go_away

    async def _drain_inbound(self, session: Any) -> None:
        """Best-effort delivery of already-received audio before teardown.

        On go_away this is skipped on purpose: the audio stays queued and the next
        session gets it, which is what keeps a reconnect seamless.
        """
        while not self._inbound.empty():
            audio = self._inbound.get_nowait()
            try:
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=telnyx_to_gemini(audio), mime_type=GEMINI_INPUT_MIME
                    )
                )
            except Exception:
                # Session already gone: put it back for whoever comes next. Broad on
                # purpose -- a closed socket surfaces as any number of driver errors,
                # and losing the caller's audio is worse than any of them.
                log.debug("could not drain inbound audio into a closing session", exc_info=True)
                self._inbound.put_nowait(audio)
                return

    async def _pump_inbound(self, session: Any) -> None:
        while True:
            audio = await self._inbound.get()
            await session.send_realtime_input(
                audio=types.Blob(data=telnyx_to_gemini(audio), mime_type=GEMINI_INPUT_MIME)
            )

    async def _on_gemini_message(self, session: Any, message: types.LiveServerMessage) -> None:
        if message.session_resumption_update is not None:
            update = message.session_resumption_update
            if update.resumable and update.new_handle:
                self.resumption_handle = update.new_handle

        content = message.server_content
        if content is not None:
            if content.interrupted:
                await self._handle_interruption()

            if content.input_transcription and content.input_transcription.text:
                self._append_transcript("caller", content.input_transcription.text)
            if content.output_transcription and content.output_transcription.text:
                self._append_transcript("agent", content.output_transcription.text)

            if content.model_turn is not None:
                for part in content.model_turn.parts or []:
                    blob = getattr(part, "inline_data", None)
                    if blob is not None and blob.data:
                        for frame in frame_20ms(gemini_to_telnyx(blob.data)):
                            self._outbound.put_nowait(frame)

        if message.tool_call is not None:
            for call in message.tool_call.function_calls or []:
                result = await self._invoke_tool_safely(call.name, dict(call.args or {}))
                await session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(id=call.id, name=call.name, response=result)
                    ]
                )

    async def _invoke_tool_safely(self, name: str, args: dict) -> dict:
        """Never let a tool failure end the call.

        The model gets a structured error it can apologise about and offer a transfer,
        which is far better than the line going dead.
        """
        if self._tool_handler is None:
            return {"error": "no tool handler configured"}
        context = self._context or ToolContext(caller_number="", call_control_id="")
        try:
            return await self._tool_handler(name, args, context)
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {
                "error": f"{name} failed: {exc}",
                "recovery": "Apologise briefly and offer to transfer to a person.",
            }

    # --- bookkeeping ----------------------------------------------------------

    def _append_transcript(self, role: str, text: str) -> None:
        self._outcome.transcript.append({"role": role, "text": text})

    def _resolve_ended_reason(self) -> str:
        if self._unsupported_codec:
            return "unsupported_codec"
        if self._saw_stop:
            return "telnyx_stop"
        if self._hit_reconnect_limit:
            return "reconnect_limit"
        return "telnyx_closed"
