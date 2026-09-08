"""Audio conversion between Telnyx and Gemini.

The riskiest line in the whole project lives here. Telnyx L16 is **big-endian**
(RFC 3551, network byte order); Gemini PCM16 is **little-endian**. Skip the swap and
you get loud static rather than silence, which is easy to misread as a codec or
sample-rate problem and hard to debug over a phone line.
"""

from __future__ import annotations

import numpy as np
import pytest

from clinic_agent.audio.codec import (
    FRAME_BYTES,
    GEMINI_OUTPUT_RATE,
    TELNYX_SAMPLE_RATE,
    frame_20ms,
    gemini_to_telnyx,
    swap_endianness,
    telnyx_to_gemini,
)


def test_frame_size_is_20ms_of_16khz_mono_pcm16():
    assert FRAME_BYTES == TELNYX_SAMPLE_RATE * 2 * 20 // 1000 == 640


def test_swap_endianness_is_its_own_inverse():
    raw = bytes(range(64))
    assert swap_endianness(swap_endianness(raw)) == raw


def test_big_endian_input_is_reinterpreted_as_little_endian():
    """256 encoded big-endian is b'\\x01\\x00'; little-endian it is b'\\x00\\x01'."""
    big_endian = np.array([256, -2, 32767], dtype=">i2").tobytes()
    out = telnyx_to_gemini(big_endian)
    assert np.frombuffer(out, dtype="<i2").tolist() == [256, -2, 32767]


def test_gemini_output_is_encoded_big_endian_for_telnyx():
    little_endian = np.array([1000, -1000], dtype="<i2").tobytes()
    out = gemini_to_telnyx(little_endian)
    # Values change through resampling, but the byte order must be big-endian.
    assert len(out) % 2 == 0
    as_big = np.frombuffer(out, dtype=">i2")
    as_little = np.frombuffer(out, dtype="<i2")
    assert abs(int(as_big[0])) < abs(int(as_little[0])), "output should read sanely as big-endian"


def test_odd_length_buffer_is_rejected():
    """A truncated frame means we lost sync; guessing would emit garbage audio."""
    with pytest.raises(ValueError, match="even number of bytes"):
        telnyx_to_gemini(b"\x01\x02\x03")


def test_resampling_converts_24khz_to_16khz_sample_count():
    one_second_at_24k = np.zeros(GEMINI_OUTPUT_RATE, dtype="<i2").tobytes()
    out = gemini_to_telnyx(one_second_at_24k)
    assert len(out) // 2 == TELNYX_SAMPLE_RATE, "one second in, one second out"


def test_resampling_preserves_pitch():
    """Guards against a resampler that silently changes playback speed.

    A wrong up/down ratio still produces plausible-looking audio -- it just sounds
    like a chipmunk or a drawl. Only a frequency check catches it.
    """
    t = np.arange(GEMINI_OUTPUT_RATE) / GEMINI_OUTPUT_RATE
    tone = (np.sin(2 * np.pi * 440 * t) * 20000).astype("<i2")

    out = gemini_to_telnyx(tone.tobytes())
    samples = np.frombuffer(out, dtype=">i2").astype(float)

    spectrum = np.abs(np.fft.rfft(samples))
    peak_hz = np.fft.rfftfreq(len(samples), 1 / TELNYX_SAMPLE_RATE)[spectrum.argmax()]
    assert abs(peak_hz - 440) < 5, f"pitch shifted to {peak_hz:.1f} Hz"


def test_full_scale_audio_does_not_wrap_around():
    """Polyphase resampling overshoots. Without clipping, a loud passage wraps from
    +32767 to a large negative value, which sounds like a violent crackle."""
    loud = np.full(GEMINI_OUTPUT_RATE // 10, 32767, dtype="<i2").tobytes()
    samples = np.frombuffer(gemini_to_telnyx(loud), dtype=">i2")
    assert samples.min() > 0, "clipping failed: loud audio wrapped to negative"
    assert samples.max() <= 32767


def test_framing_splits_into_exact_20ms_frames():
    frames = frame_20ms(b"\x00" * (FRAME_BYTES * 3))
    assert len(frames) == 3
    assert all(len(f) == FRAME_BYTES for f in frames)


def test_framing_pads_a_partial_tail_rather_than_dropping_it():
    """Telnyx rejects chunks under 20 ms, and dropping the tail clips the last word."""
    frames = frame_20ms(b"\x01" * (FRAME_BYTES + 100))
    assert len(frames) == 2
    assert all(len(f) == FRAME_BYTES for f in frames)
    assert frames[1][:100] == b"\x01" * 100
    assert frames[1][100:] == b"\x00" * (FRAME_BYTES - 100)


def test_framing_empty_input_yields_no_frames():
    assert frame_20ms(b"") == []


def test_silence_survives_the_round_trip_as_silence():
    silence = np.zeros(GEMINI_OUTPUT_RATE // 10, dtype="<i2").tobytes()
    assert set(gemini_to_telnyx(silence)) == {0}
