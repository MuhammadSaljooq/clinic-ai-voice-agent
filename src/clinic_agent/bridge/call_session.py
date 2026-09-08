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

# Sent once when the first session opens. Without it the model waits for the caller to
# speak, so a real caller hears silence after the line connects -- and the system
# instruction's "greet first, disclose you are an AI" never happens.
GREETING_NUDGE = (
    "(The phone call has just connected and the caller is listening. "
    "Greet them now, in one short sentence, and state that you are an AI assistant.)"
)

# How long to let the Gemini loop finish after the call ends before giving up on it.
DRAIN_TIMEOUT_SECONDS = 10.0


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
        greet_on_connect: bool = True,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    ):
        self._telnyx = telnyx
        self._connect_gemini = connect_gemini
        self._tool_handler = tool_handler
        self._max_reconnects = max_reconnects
        self._sleep = sleep
        self._greet_on_connect = greet_on_connect
        self._drain_timeout = drain_timeout
        self._greeted = False

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
        self._audio_in_frames = 0
        self._audio_out_frames = 0

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
                try:
                    # Bounded: session.receive() blocks until Gemini yields or closes,
                    # and a session that does neither would hang the call forever.
                    await asyncio.wait_for(asyncio.shield(gemini), timeout=self._drain_timeout)
                except TimeoutError:
                    log.warning(
                        "Gemini loop did not finish within %.0fs of the call ending; cancelling",
                        self._drain_timeout,
                    )
                    gemini.cancel()
                    await asyncio.gather(gemini, return_exceptions=True)
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
                log.info(
                    "opening Gemini session (handle=%s)",
                    "resume" if self.resumption_handle else "new",
                )
                async with self._connect_gemini(self.resumption_handle) as session:
                    log.info("Gemini session open")
                    reconnect_requested = await self._serve_session(session)
                    log.info(
                        "Gemini session closed (audio in=%d frames, out=%d frames)",
                        self._audio_in_frames,
                        self._audio_out_frames,
                    )
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
            if self._greet_on_connect and not self._greeted:
                self._greeted = True
                log.info("prompting the agent to greet the caller")
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": GREETING_NUDGE}]},
                    turn_complete=True,
                )

            # `session.receive()` completes at each TURN boundary, not when the socket
            # closes -- so it must be re-entered for every turn on the same session.
            # Treating its completion as a dropped connection reconnects after the
            # first turn and kills the conversation, which is exactly what it did.
            empty_turns = 0
            # do-while: always make one pass, even if the caller has already hung up,
            # so audio and tool responses already in flight are still drained.
            while True:
                messages = 0
                async for message in session.receive():
                    messages += 1
                    if message.go_away is not None:
                        # Google warns before resetting the socket, so we can
                        # reconnect on our own terms rather than mid-sentence.
                        log.info("Gemini go_away received; reconnecting")
                        go_away = True
                        break
                    await self._on_gemini_message(session, message)

                if go_away or self._call_ended.is_set():
                    break

                if messages:
                    empty_turns = 0
                    continue

                # A genuinely closed socket yields nothing, repeatedly. Distinguish
                # that from an idle turn boundary without spinning hot.
                empty_turns += 1
                if empty_turns >= 3:
                    log.info("Gemini yielded nothing three times; treating as closed")
                    break
                await self._sleep(0.05)
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
            self._audio_in_frames += 1
            if self._audio_in_frames == 1:
                log.info("first caller audio forwarded to Gemini")

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
                            self._audio_out_frames += 1
                        if self._audio_out_frames and self._audio_in_frames >= 0:
                            log.debug("queued agent audio (%d frames total)", self._audio_out_frames)

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
