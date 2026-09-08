"""Drive the running voice agent as if we were Telnyx, with no phone involved.

Connects to the app's own WebSocket endpoint, synthesizes the caller's speech with
Gemini TTS, streams it in exactly as Telnyx would (L16 big-endian, 16 kHz, 20 ms
frames), and writes whatever the agent says back to a WAV file.

Tests everything except Telnyx itself: the codec, the bridge, barge-in, tool calls,
real Gemini, real Postgres.

It also grades the returned audio automatically. Speech has strongly correlated
adjacent samples; a byte-order mistake scrambles the low byte into the high byte and
drops that correlation to near zero. So the harness can tell you it sounds like static
without anyone having to listen.

Usage:
  .venv/bin/python scripts/simulate_call.py \
      --say "Hi, I'd like to book a follow-up appointment" \
      --say "Yes, the first one works. My name is Ada Lovelace"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import pathlib
import sys
import time
import wave

import numpy as np
import websockets
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

import os

from google import genai
from google.genai import types

from clinic_agent.audio.codec import (
    FRAME_BYTES,
    TELNYX_SAMPLE_RATE,
    gemini_to_telnyx,
    telnyx_to_gemini,
)

FRAME_SECONDS = 0.02
TTS_MODEL = "gemini-2.5-flash-preview-tts"
CALLER_VOICE = "Puck"


CACHE = ROOT / ".tts-cache"


def synthesize(text: str) -> bytes:
    """Caller speech as Telnyx would deliver it: L16 big-endian, 16 kHz.

    Gemini TTS returns 24 kHz little-endian, which is exactly what the production
    outbound codec converts from -- so the caller direction reuses `gemini_to_telnyx`.

    Cached on disk by phrase. The free tier allows only 10 TTS requests per DAY, and
    re-synthesizing an identical line on every run exhausts that in one sitting.
    """
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / (hashlib.sha256(f"{CALLER_VOICE}|{text}".encode()).hexdigest()[:24] + ".raw")
    if cached.exists():
        print(f"  (cached) {text[:50]!r}")
        return cached.read_bytes()

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=f"Say this naturally, like a patient phoning a clinic: {text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=CALLER_VOICE)
                )
            ),
        ),
    )
    pcm_24k_le = response.candidates[0].content.parts[0].inline_data.data
    wire = gemini_to_telnyx(pcm_24k_le)
    cached.write_bytes(wire)
    return wire


def grade_audio(pcm_16k_be: bytes) -> tuple[bool, str]:
    """Does this look like speech, or like a byte-order accident?"""
    if len(pcm_16k_be) < FRAME_BYTES * 5:
        return False, "too little audio to judge"
    samples = np.frombuffer(pcm_16k_be, dtype=">i2").astype(np.float64)
    if samples.std() < 1:
        return False, "silence"
    correlation = float(np.corrcoef(samples[:-1], samples[1:])[0, 1])
    roughness = float(np.abs(np.diff(samples)).mean() / (samples.std() + 1e-9))
    verdict = correlation > 0.5
    return verdict, f"lag1_corr={correlation:+.4f} roughness={roughness:.3f}"


def write_wav(path: pathlib.Path, pcm_16k_be: bytes) -> float:
    """WAV wants little-endian, the wire gave us big-endian."""
    pcm_le = telnyx_to_gemini(pcm_16k_be)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(TELNYX_SAMPLE_RATE)
        out.writeframes(pcm_le)
    return len(pcm_le) / 2 / TELNYX_SAMPLE_RATE


def start_frame(caller: str) -> str:
    return json.dumps({
        "event": "start",
        "stream_id": "sim-1",
        "start": {
            "call_control_id": "sim-ccid-1",
            "call_session_id": "sim-csid-1",
            "from": caller,
            "to": "+15550222",
            "media_format": {"encoding": "L16", "sample_rate": 16000, "channels": 1},
        },
    })


async def run(args) -> int:
    print(f"Synthesizing {len(args.say)} caller utterance(s) with {TTS_MODEL}...")
    utterances = [synthesize(text) for text in args.say]
    for text, audio in zip(args.say, utterances, strict=True):
        print(f"  {len(audio) / 2 / TELNYX_SAMPLE_RATE:5.2f}s  \"{text}\"")

    received = bytearray()
    clears = 0
    first_audio_at: float | None = None
    caller_done_at: float | None = None
    latencies: list[float] = []

    print(f"\nConnecting to {args.url} ...")
    async with websockets.connect(args.url, max_size=None) as ws:
        print("  connected\n")

        async def receiver():
            nonlocal clears, first_audio_at
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("event") == "media":
                        if first_audio_at is None and caller_done_at is not None:
                            first_audio_at = time.perf_counter()
                            latencies.append(first_audio_at - caller_done_at)
                        received.extend(base64.b64decode(frame["media"]["payload"]))
                    elif frame.get("event") == "clear":
                        clears += 1
                        print("  <- clear (barge-in)")
            except websockets.ConnectionClosed:
                pass

        reader = asyncio.create_task(receiver())
        await ws.send(start_frame(args.caller))

        silence = b"\x00" * FRAME_BYTES

        async def stream(frames: list[bytes]) -> None:
            for frame in frames:
                padded = frame + b"\x00" * (FRAME_BYTES - len(frame))
                await ws.send(
                    json.dumps({
                        "event": "media",
                        "media": {"payload": base64.b64encode(padded).decode()},
                    })
                )
                await asyncio.sleep(FRAME_SECONDS)  # real-time pacing, so VAD behaves

        for index, audio in enumerate(utterances, start=1):
            print(f"  -> speaking utterance {index}")
            await stream([audio[i : i + FRAME_BYTES] for i in range(0, len(audio), FRAME_BYTES)])

            # Reset per utterance: one figure for the whole call is meaningless, and
            # an earlier version produced a negative number by mixing turns.
            first_audio_at = None
            caller_done_at = time.perf_counter()
            before = len(received)
            print(f"  .. listening {args.listen}s (streaming silence, as Telnyx does)")

            # Crucial: a real call never stops sending. Gemini ends the caller's turn
            # only after hearing `silence_duration_ms` of actual silence, so cutting the
            # stream dead means the turn never completes and the model never replies.
            await stream([silence] * int(args.listen / FRAME_SECONDS))

            gained = len(received) - before
            print(f"     received {gained / 2 / TELNYX_SAMPLE_RATE:.2f}s of agent audio")

        await ws.send(json.dumps({"event": "stop", "stream_id": "sim-1",
                                  "stop": {"call_control_id": "sim-ccid-1"}}))
        await asyncio.sleep(0.5)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    print("\n" + "=" * 64)
    if not received:
        print("RESULT: the agent sent no audio at all.")
        print("Check the app log -- the Gemini session probably failed to open.")
        return 1

    out = pathlib.Path(args.out)
    seconds = write_wav(out, bytes(received))
    sounds_like_speech, detail = grade_audio(bytes(received))

    print(f"agent audio      : {seconds:.2f}s  ({len(received)} bytes)")
    print(f"barge-in clears  : {clears}")
    if latencies:
        pretty = ", ".join(f"{v * 1000:.0f} ms" for v in latencies)
        print(f"reply latency    : {pretty}   (end of caller speech -> first agent audio)")
        print("                   target is under 800 ms for a natural-feeling call")
    else:
        print("reply latency    : not measured (no audio arrived after a caller turn)")
    print(f"audio sanity     : {'SPEECH' if sounds_like_speech else 'NOT SPEECH'}  {detail}")
    if not sounds_like_speech:
        print("  ^ near-zero correlation means a byte-order or codec fault: static, not words.")
    print(f"saved            : {out}")
    print("=" * 64)
    return 0 if sounds_like_speech else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--say", action="append", required=True,
                        help="What the caller says. Repeat for multiple turns.")
    parser.add_argument("--url", default="ws://localhost:8080/telnyx/stream")
    parser.add_argument("--caller", default="+17025550199")
    parser.add_argument("--listen", type=float, default=8.0,
                        help="Seconds to wait for a reply after each utterance")
    parser.add_argument("--out", default="agent_reply.wav")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
