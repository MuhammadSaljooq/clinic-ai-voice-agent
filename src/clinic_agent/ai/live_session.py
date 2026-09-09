"""Production Gemini Live connector.

Small by design: the interesting logic lives in `live_config` (pure, tested) and
`bridge/call_session` (tested against fakes). This is the thin piece that needs real
credentials, so it is the piece that cannot be unit-tested.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from google.genai import types

from clinic_agent.ai.live_config import (
    DEFAULT_VOICE,
    NATIVE_AUDIO_MODEL,
    VadTuning,
    build_live_config,
)
from clinic_agent.ai.provider import ProviderSettings, build_client
from clinic_agent.config import ClinicConfig

log = logging.getLogger(__name__)


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def vad_from_env(env: Mapping[str, str] | None = None) -> VadTuning:
    """Resolve turn-taking tuning from the environment, falling back to the snappy
    defaults. Bad values are ignored rather than crashing a deploy over a typo."""
    src = os.environ if env is None else env
    base = VadTuning()

    def _int(name: str, default: int) -> int:
        raw = src.get(name)
        if raw is None:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            log.warning("ignoring non-integer %s=%r", name, raw)
            return default

    def _sensitivity(name: str, high, low, default):
        raw = (src.get(name) or "").strip().lower()
        if raw == "high":
            return high
        if raw == "low":
            return low
        return default

    return VadTuning(
        silence_ms=_int("VAD_SILENCE_MS", base.silence_ms),
        prefix_padding_ms=_int("VAD_PREFIX_PADDING_MS", base.prefix_padding_ms),
        end_sensitivity=_sensitivity(
            "VAD_END_SENSITIVITY",
            types.EndSensitivity.END_SENSITIVITY_HIGH,
            types.EndSensitivity.END_SENSITIVITY_LOW,
            base.end_sensitivity,
        ),
        start_sensitivity=_sensitivity(
            "VAD_START_SENSITIVITY",
            types.StartSensitivity.START_SENSITIVITY_HIGH,
            types.StartSensitivity.START_SENSITIVITY_LOW,
            base.start_sensitivity,
        ),
        interruptible=_env_bool(src.get("AGENT_INTERRUPTIBLE"), base.interruptible),
    )


def build_gemini_connector(
    cfg: ClinicConfig,
    settings: ProviderSettings | None = None,
    *,
    model: str | None = None,
    voice: str | None = None,
    vad: VadTuning | None = None,
    enable_affective_dialog: bool | None = None,
) -> Callable[[str | None], Any]:
    """Return `connect(resumption_handle) -> async context manager` for CallSession.

    Model, VAD tuning and affective dialog are read from the environment here (the
    impure seam) so the config builder stays pure. Affective dialog reads the caller's
    tone but costs latency; AI_ENABLE_AFFECTIVE_DIALOG=false trades it for speed.
    GEMINI_LIVE_MODEL swaps the model -- the half-cascade FALLBACK_MODEL replies faster
    than the richer native-audio default.
    """
    client = build_client(settings)
    model = model or os.environ.get("GEMINI_LIVE_MODEL") or NATIVE_AUDIO_MODEL
    voice = voice or os.environ.get("GEMINI_VOICE") or DEFAULT_VOICE
    vad = vad or vad_from_env()
    if enable_affective_dialog is None:
        enable_affective_dialog = _env_bool(os.environ.get("AI_ENABLE_AFFECTIVE_DIALOG"), True)
    log.info(
        "voice: model=%s voice=%s turn-taking silence=%dms prefix=%dms end=%s affective=%s",
        model, voice, vad.silence_ms, vad.prefix_padding_ms, vad.end_sensitivity, enable_affective_dialog,
    )

    def connect(resumption_handle: str | None):
        return client.aio.live.connect(
            model=model,
            config=build_live_config(
                cfg,
                resumption_handle=resumption_handle,
                voice=voice,
                vad=vad,
                enable_affective_dialog=enable_affective_dialog,
            ),
        )

    return connect
