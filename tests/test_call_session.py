"""The voice bridge: Telnyx call <-> Gemini Live session.

Driven entirely by fakes, so every behaviour that matters on a real call -- byte
order, framing, barge-in, tool dispatch, reconnect across the ~10-minute WebSocket
reset -- is verified with no network and no credentials.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from fakes import (
    FakeConnector,
    FakeGeminiSession,
    FakeTelnyxSocket,
    gemini_audio,
    gemini_go_away,
    gemini_input_transcript,
    gemini_interrupted,
    gemini_output_transcript,
    gemini_resumption,
    gemini_tool_call,
    telnyx_dtmf,
    telnyx_media,
    telnyx_start,
    telnyx_stop,
)

from clinic_agent.audio.codec import FRAME_BYTES, telnyx_to_gemini
from clinic_agent.bridge.call_session import CallSession, ToolContext


def big_endian(values: list[int]) -> bytes:
    """Audio as Telnyx sends it: L16, big-endian."""
    return np.array(values, dtype=">i2").tobytes()


def gemini_pcm(milliseconds: int) -> bytes:
    """Audio as Gemini sends it: PCM16 little-endian at 24 kHz."""
    samples = 24_000 * milliseconds // 1000
    tone = (np.sin(np.arange(samples) / 8) * 8000).astype("<i2")
    return tone.tobytes()


async def noop_sleep(_seconds: float) -> None:
    """Removes real-time pacing so tests are instant but still exercise the writer."""
    return


async def run_session(
    script,
    gemini_sessions,
    *,
    handler=None,
    max_reconnects=0,
):
    calls: list[tuple[str, dict]] = []

    async def default_handler(name, args, ctx):
        calls.append((name, args))
        return {"ok": True}

    # A real call stays connected until it hangs up. So unless the script explicitly
    # ends with a stop frame, hold the socket open and let the Gemini side decide when
    # the test is over -- otherwise running out of scripted frames would masquerade as
    # the caller hanging up.
    ends_with_stop = any('"event": "stop"' in f or '"event":"stop"' in f for f in script)
    socket = FakeTelnyxSocket(script, hold_open=not ends_with_stop)
    connector = FakeConnector(*gemini_sessions)
    session = CallSession(
        telnyx=socket,
        connect_gemini=connector,
        tool_handler=handler or default_handler,
        max_reconnects=max_reconnects,
        sleep=noop_sleep,
    )
    outcome = await session.run()
    return socket, connector, session, outcome, calls


# --- audio path ---------------------------------------------------------------


async def test_inbound_audio_reaches_gemini_byte_swapped_and_not_resampled():
    """Telnyx L16 is big-endian; Gemini wants little-endian at the same 16 kHz."""
    audio = big_endian([256, -2, 32767, 0])
    gemini = FakeGeminiSession()

    await run_session([telnyx_start(), telnyx_media(audio), telnyx_stop()], [gemini])

    assert len(gemini.audio_in) == 1
    assert gemini.audio_in[0].data == telnyx_to_gemini(audio)
    assert gemini.audio_in[0].data != audio, "byte swap did not happen"
    assert "16000" in gemini.audio_in[0].mime_type


async def test_gemini_audio_reaches_telnyx_as_exact_20ms_frames():
    """100 ms at 24 kHz becomes 100 ms at 16 kHz, which is five 640-byte frames."""
    gemini = FakeGeminiSession([gemini_audio(gemini_pcm(100))])

    socket, *_ = await run_session([telnyx_start()], [gemini])

    assert socket.media_frames, "no audio was sent to Telnyx"
    assert all(len(f) == FRAME_BYTES for f in socket.media_frames)
    assert len(socket.media_frames) == 5


async def test_outbound_audio_is_big_endian_for_telnyx():
    gemini = FakeGeminiSession([gemini_audio(gemini_pcm(40))])
    socket, *_ = await run_session([telnyx_start()], [gemini])

    samples_be = np.frombuffer(socket.sent_audio, dtype=">i2")
    samples_le = np.frombuffer(socket.sent_audio, dtype="<i2")
    assert np.abs(samples_be).mean() < np.abs(samples_le).mean(), (
        "audio only reads sanely as big-endian if the swap happened"
    )


# --- barge-in ------------------------------------------------------------------


async def test_interruption_tells_telnyx_to_clear_buffered_audio():
    """Clearing only our own queue still leaves speech in Telnyx's buffer, which is
    the most robot-like failure a voice agent has."""
    gemini = FakeGeminiSession([gemini_audio(gemini_pcm(200)), gemini_interrupted()])

    socket, *_ = await run_session([telnyx_start()], [gemini])

    assert "clear" in socket.sent_events


async def test_interruption_also_drops_audio_we_have_not_sent_yet():
    socket = FakeTelnyxSocket([], hold_open=True)
    session = CallSession(
        telnyx=socket,
        connect_gemini=FakeConnector(),
        tool_handler=None,
        sleep=noop_sleep,
    )
    for _ in range(5):
        session._outbound.put_nowait(b"\x00" * FRAME_BYTES)

    await session._handle_interruption()

    assert session._outbound.empty(), "queued audio survived an interruption"
    assert json.loads(socket.sent[-1]) == {"event": "clear"}


# --- tools ---------------------------------------------------------------------


async def test_tool_call_is_dispatched_and_answered_with_a_matching_id():
    gemini = FakeGeminiSession([gemini_tool_call("answer_faq", {"question": "hours"}, "fc-9")])

    _, _, _, _, calls = await run_session([telnyx_start()], [gemini])

    assert calls == [("answer_faq", {"question": "hours"})]
    assert len(gemini.tool_responses) == 1
    response = gemini.tool_responses[0]
    assert response.id == "fc-9"
    assert response.name == "answer_faq"
    assert response.response == {"ok": True}


async def test_a_failing_tool_returns_an_error_instead_of_dropping_the_call():
    """The model should get something it can apologise about, not a dead line."""
    async def exploding_handler(name, args, ctx):
        raise RuntimeError("database is on fire")

    gemini = FakeGeminiSession([gemini_tool_call("find_slots", {}, "fc-1")])

    _, _, _, outcome, _ = await run_session(
        [telnyx_start()], [gemini], handler=exploding_handler
    )

    assert len(gemini.tool_responses) == 1
    assert "error" in gemini.tool_responses[0].response
    assert outcome.ended_reason != "crash"


async def test_tool_handler_receives_the_caller_number():
    """Plan 3 needs caller ID so it does not have to ask for a phone number."""
    seen: list[ToolContext] = []

    async def capturing(name, args, ctx):
        seen.append(ctx)
        return {}

    gemini = FakeGeminiSession([gemini_tool_call("lookup_appointment", {}, "fc-1")])
    await run_session(
        [telnyx_start(from_number="+15559999")], [gemini], handler=capturing
    )

    assert seen[0].caller_number == "+15559999"
    assert seen[0].call_control_id == "ccid-1"


# --- session continuity --------------------------------------------------------


async def test_resumption_handle_is_stored():
    gemini = FakeGeminiSession([gemini_resumption("handle-1")])
    _, _, session, _, _ = await run_session([telnyx_start()], [gemini])
    assert session.resumption_handle == "handle-1"


async def test_go_away_reconnects_using_the_stored_handle():
    """The Live socket resets around ten minutes -- shorter than a bad phone call."""
    first = FakeGeminiSession([gemini_resumption("handle-1"), gemini_go_away()])
    second = FakeGeminiSession([gemini_audio(gemini_pcm(40))])

    socket, connector, _, outcome, _ = await run_session(
        [telnyx_start()], [first, second], max_reconnects=1
    )

    assert connector.handles == [None, "handle-1"], "reconnect must reuse the handle"
    assert outcome.reconnects == 1
    assert socket.media_frames, "audio must keep flowing after the reconnect"


async def test_inbound_audio_is_not_lost_across_a_reconnect():
    """Telnyx keeps sending during the gap; that audio must be buffered, not dropped."""
    first = FakeGeminiSession([gemini_go_away()])
    second = FakeGeminiSession()

    _, _, _, _, _ = await run_session(
        [telnyx_start(), telnyx_media(big_endian([1, 2, 3, 4]))],
        [first, second],
        max_reconnects=1,
    )

    delivered = len(first.audio_in) + len(second.audio_in)
    assert delivered == 1, f"audio was lost or duplicated across reconnect ({delivered})"


async def test_reconnects_are_capped_so_a_broken_session_cannot_loop_forever():
    gemini = FakeGeminiSession([gemini_go_away()])
    _, connector, _, outcome, _ = await run_session(
        [telnyx_start()], [gemini], max_reconnects=0
    )
    assert connector.handles == [None]
    assert outcome.ended_reason == "reconnect_limit"


# --- call lifecycle ------------------------------------------------------------


async def test_stop_event_ends_the_call_cleanly():
    _, _, _, outcome, _ = await run_session(
        [telnyx_start(), telnyx_stop()], [FakeGeminiSession()]
    )
    assert outcome.ended_reason == "telnyx_stop"
    assert outcome.call_control_id == "ccid-1"


async def test_pressing_zero_transfers_to_a_human_without_asking_the_model():
    """DTMF is handled outside the model so the escape hatch always works."""
    _, _, _, _, calls = await run_session(
        [telnyx_start(), telnyx_dtmf("0"), telnyx_stop()], [FakeGeminiSession()]
    )
    assert any(name == "transfer_to_human" for name, _ in calls)


async def test_an_unknown_telnyx_event_does_not_drop_the_call():
    script = [telnyx_start(), json.dumps({"event": "brand_new_thing"}), telnyx_stop()]
    _, _, _, outcome, _ = await run_session(script, [FakeGeminiSession()])
    assert outcome.ended_reason == "telnyx_stop"


async def test_a_malformed_frame_does_not_drop_the_call():
    script = [telnyx_start(), "{not json at all", telnyx_stop()]
    _, _, _, outcome, _ = await run_session(script, [FakeGeminiSession()])
    assert outcome.ended_reason == "telnyx_stop"


async def test_transcripts_from_both_directions_are_captured():
    gemini = FakeGeminiSession([
        gemini_input_transcript("do you have anything tuesday"),
        gemini_output_transcript("let me take a look for you"),
    ])
    _, _, _, outcome, _ = await run_session([telnyx_start()], [gemini])

    roles = [entry["role"] for entry in outcome.transcript]
    texts = " ".join(entry["text"] for entry in outcome.transcript)
    assert "caller" in roles
    assert "agent" in roles
    assert "tuesday" in texts
    assert "take a look" in texts


async def test_audio_before_the_start_frame_is_ignored():
    """Media can only be interpreted once we know the stream's media format."""
    gemini = FakeGeminiSession()
    await run_session(
        [telnyx_media(big_endian([1, 2])), telnyx_start(), telnyx_stop()], [gemini]
    )
    assert gemini.audio_in == []


@pytest.mark.parametrize("encoding", ["PCMU", "OPUS"])
async def test_an_unexpected_codec_is_refused_rather_than_played_as_noise(encoding):
    """If Telnyx negotiates something other than L16 we would emit garbage; refuse."""
    start = json.loads(telnyx_start())
    start["start"]["media_format"]["encoding"] = encoding
    _, _, _, outcome, _ = await run_session(
        [json.dumps(start)], [FakeGeminiSession()]
    )
    assert outcome.ended_reason == "unsupported_codec"


# --- greeting ------------------------------------------------------------------


async def test_the_agent_is_prompted_to_greet_when_the_call_connects():
    """Without this the model waits for the caller, so a real caller hears silence
    instead of a greeting."""
    gemini = FakeGeminiSession()
    await run_session([telnyx_start(), telnyx_stop()], [gemini])

    assert len(gemini.client_content) == 1
    nudge = str(gemini.client_content[0])
    assert "greet" in nudge.lower()


async def test_the_greeting_is_not_repeated_after_a_reconnect():
    """Re-greeting mid-call would have the agent introduce itself twice."""
    first = FakeGeminiSession([gemini_resumption("h1"), gemini_go_away()])
    second = FakeGeminiSession()

    await run_session([telnyx_start()], [first, second], max_reconnects=1)

    assert len(first.client_content) == 1
    assert second.client_content == [], "should not greet again on the resumed session"


async def test_greeting_can_be_disabled():
    socket = FakeTelnyxSocket([telnyx_start(), telnyx_stop()])
    gemini = FakeGeminiSession()
    session = CallSession(
        telnyx=socket,
        connect_gemini=FakeConnector(gemini),
        tool_handler=None,
        max_reconnects=0,
        sleep=noop_sleep,
        greet_on_connect=False,
    )
    await session.run()
    assert gemini.client_content == []


async def test_a_turn_boundary_is_not_mistaken_for_a_dropped_session():
    """Regression: session.receive() completes at each TURN, not on socket close.

    The first implementation treated its completion as a dropped connection and
    reconnected, so every conversation died after one turn. Verified against the real
    API: calling receive() again on the same session returns the next turn's audio.
    """
    gemini = FakeGeminiSession(turns=[
        [gemini_audio(gemini_pcm(40))],   # turn 1
        [gemini_audio(gemini_pcm(40))],   # turn 2, same session
    ])
    socket, connector, _, _, _ = await run_session(
        [telnyx_start()], [gemini], max_reconnects=0
    )

    assert connector.handles == [None], "must not reconnect between turns"
    assert gemini.receive_calls >= 2, "receive() must be re-entered for the next turn"
    # Both turns' audio reached Telnyx: 40ms at 24k -> 40ms at 16k -> 2 frames each.
    assert len(socket.media_frames) == 4


async def test_transcript_fragments_from_one_speaker_are_merged():
    """Gemini streams transcription in pieces; one line per fragment is unreadable."""
    gemini = FakeGeminiSession([
        gemini_output_transcript("Thanks for"),
        gemini_output_transcript(" calling"),
        gemini_output_transcript(" Northside."),
        gemini_input_transcript("I'd like"),
        gemini_input_transcript(" an appointment"),
        gemini_output_transcript("Sure"),
    ])
    _, _, _, outcome, _ = await run_session([telnyx_start()], [gemini], max_reconnects=0)

    assert [e["role"] for e in outcome.transcript] == ["agent", "caller", "agent"]
    assert outcome.transcript[0]["text"] == "Thanks for calling Northside."
    assert outcome.transcript[1]["text"] == "I'd like an appointment"


