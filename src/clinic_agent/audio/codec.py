"""Audio conversion between Telnyx and Gemini Live.

Two independent transformations, deliberately kept separate so each is testable:

**Byte order.** Telnyx L16 is big-endian -- RFC 3551 specifies network byte order,
most significant byte first. Gemini Live wants little-endian PCM16. Omit the swap and
every sample is multiplied by roughly 256 and sign-flipped, which comes out as loud
static rather than silence. That failure looks like a codec or sample-rate mismatch,
so it is worth stating plainly: this is a byte-order problem, and the swap is required
in *both* directions.

**Sample rate.** Inbound needs none: Telnyx L16 is 16 kHz and Gemini accepts 16 kHz,
so the hot path is a byte swap and nothing else. Outbound resamples Gemini's 24 kHz
down to 16 kHz at a clean 2/3 ratio.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly

TELNYX_SAMPLE_RATE = 16_000
GEMINI_INPUT_RATE = 16_000
GEMINI_OUTPUT_RATE = 24_000

FRAME_MS = 20
BYTES_PER_SAMPLE = 2
FRAME_BYTES = TELNYX_SAMPLE_RATE * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 640

# 24 kHz -> 16 kHz is exactly 2/3, so no rounding drift accumulates over a long call.
_RESAMPLE_UP = 2
_RESAMPLE_DOWN = 3

_INT16_MIN = -32768
_INT16_MAX = 32767


def _check_even(raw: bytes) -> None:
    if len(raw) % BYTES_PER_SAMPLE:
        raise ValueError(
            f"PCM16 requires an even number of bytes, got {len(raw)}; "
            "an odd length means frame sync was lost"
        )


def swap_endianness(raw: bytes) -> bytes:
    """Reverse the byte order of each 16-bit sample. Its own inverse."""
    _check_even(raw)
    return np.frombuffer(raw, dtype=np.int16).byteswap().tobytes()


def telnyx_to_gemini(raw: bytes) -> bytes:
    """Telnyx L16 big-endian 16 kHz -> Gemini PCM16 little-endian 16 kHz.

    Rates already match, so this is purely a byte-order change.
    """
    _check_even(raw)
    samples = np.frombuffer(raw, dtype=">i2")
    return samples.astype("<i2").tobytes()


def gemini_to_telnyx(raw: bytes) -> bytes:
    """Gemini PCM16 little-endian 24 kHz -> Telnyx L16 big-endian 16 kHz."""
    _check_even(raw)
    if not raw:
        return b""

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    resampled = resample_poly(samples, up=_RESAMPLE_UP, down=_RESAMPLE_DOWN)

    # Polyphase filtering overshoots on transients. Without clipping, a loud passage
    # wraps from +32767 to a large negative value and sounds like a violent crackle.
    clipped = np.clip(np.rint(resampled), _INT16_MIN, _INT16_MAX)

    return clipped.astype(">i2").tobytes()


def frame_20ms(raw: bytes) -> list[bytes]:
    """Split into exactly-20 ms frames, zero-padding any partial tail.

    Telnyx rejects chunks shorter than 20 ms, and dropping the tail clips the last
    fraction of a word -- so pad rather than discard.
    """
    if not raw:
        return []
    frames = [raw[i : i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]
    last = frames[-1]
    if len(last) < FRAME_BYTES:
        frames[-1] = last + b"\x00" * (FRAME_BYTES - len(last))
    return frames
