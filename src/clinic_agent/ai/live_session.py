"""Production Gemini Live connector.

Small by design: the interesting logic lives in `live_config` (pure, tested) and
`bridge/call_session` (tested against fakes). This is the thin piece that needs real
credentials, so it is the piece that cannot be unit-tested.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clinic_agent.ai.live_config import DEFAULT_VOICE, NATIVE_AUDIO_MODEL, build_live_config
from clinic_agent.ai.provider import ProviderSettings, build_client
from clinic_agent.config import ClinicConfig


def build_gemini_connector(
    cfg: ClinicConfig,
    settings: ProviderSettings | None = None,
    *,
    model: str = NATIVE_AUDIO_MODEL,
    voice: str = DEFAULT_VOICE,
) -> Callable[[str | None], Any]:
    """Return `connect(resumption_handle) -> async context manager` for CallSession."""
    client = build_client(settings)

    def connect(resumption_handle: str | None):
        return client.aio.live.connect(
            model=model,
            config=build_live_config(cfg, resumption_handle=resumption_handle, voice=voice),
        )

    return connect
