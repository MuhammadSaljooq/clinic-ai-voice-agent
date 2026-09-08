"""Entrypoint: wires real dependencies and serves the app.

Run with:  .venv/bin/python -m clinic_agent.main
"""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib

import asyncpg
import uvicorn
from dotenv import load_dotenv

from clinic_agent.agent.tools import ToolRouter
from clinic_agent.ai.live_session import build_gemini_connector
from clinic_agent.ai.provider import settings_from_env
from clinic_agent.app import AppDeps, create_app
from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.telephony.telnyx_client import TelnyxClient

log = logging.getLogger(__name__)

MIGRATION = pathlib.Path(__file__).parent / "db" / "migrations" / "001_init.sql"


def build_app():
    # Read .env so secrets stay in a gitignored file rather than the shell history.
    load_dotenv()

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

    telnyx = TelnyxClient(os.environ["TELNYX_API_KEY"])
    deps = AppDeps(
        cfg=cfg,
        telnyx=telnyx,
        public_key_b64=os.environ["TELNYX_PUBLIC_KEY"],
        stream_url=stream_url,
        connect_gemini=build_gemini_connector(cfg, settings),
        tool_handler=None,  # filled in by the lifespan once the pool exists
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=10)
        # The migration is idempotent, so applying it on boot keeps deploys simple.
        async with pool.acquire() as conn:
            await conn.execute(MIGRATION.read_text())
        await seed_from_config(pool, cfg)

        # One router serves every call: all per-call state lives on the ToolContext,
        # so there is nothing call-specific to keep here.
        deps.tool_handler = ToolRouter(
            pool=pool,
            cfg=cfg,
            secret=os.environ["SLOT_TOKEN_SECRET"],
            telnyx=telnyx,
        )
        log.info("ready: %s, %d provider(s)", cfg.clinic.name, len(cfg.providers))
        try:
            yield
        finally:
            await pool.close()
            await telnyx.aclose()

    return create_app(deps, lifespan=lifespan)


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
