"""Entrypoint: wires real dependencies and serves the app.

Run with:  .venv/bin/python -m clinic_agent.main
"""

from __future__ import annotations

import logging
import os

import uvicorn

from clinic_agent.ai.live_session import build_gemini_connector
from clinic_agent.ai.provider import settings_from_env
from clinic_agent.app import AppDeps, create_app
from clinic_agent.bridge.call_session import ToolContext
from clinic_agent.config import load_config
from clinic_agent.telephony.telnyx_client import TelnyxClient

log = logging.getLogger(__name__)


async def not_wired_yet(name: str, args: dict, ctx: ToolContext) -> dict:
    """Placeholder until Plan 3 wires tools to the scheduler.

    Returns a structured error rather than raising, so the agent apologises and offers
    a transfer instead of the line going dead.
    """
    log.warning("tool %s called but handlers are not wired yet (Plan 3)", name)
    return {
        "error": "Scheduling tools are not connected yet.",
        "recovery": "Apologise and offer to transfer the caller to a person.",
    }


def build_app():
    cfg = load_config(os.environ.get("CLINIC_CONFIG", "config.yaml"))
    settings = settings_from_env()

    if not settings.is_baa_eligible:
        log.warning(
            "AI provider is %s, which is NOT BAA-eligible. Synthetic data only -- "
            "do not point a real patient line at this. Set AI_PROVIDER=vertex first.",
            settings.kind,
        )

    stream_url = os.environ["PUBLIC_STREAM_URL"]  # e.g. wss://your-host/telnyx/stream
    if not stream_url.startswith("wss://"):
        raise ValueError(f"PUBLIC_STREAM_URL must be wss://, got {stream_url!r}")

    return create_app(
        AppDeps(
            cfg=cfg,
            telnyx=TelnyxClient(os.environ["TELNYX_API_KEY"]),
            public_key_b64=os.environ["TELNYX_PUBLIC_KEY"],
            stream_url=stream_url,
            connect_gemini=build_gemini_connector(cfg, settings),
            tool_handler=not_wired_yet,
        )
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
