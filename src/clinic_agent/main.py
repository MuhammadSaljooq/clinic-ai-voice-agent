"""Entrypoint: wires real dependencies and serves the app.

Run with:  .venv/bin/python -m clinic_agent.main
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from clinic_agent.calendar_mirror.sync import CalendarMirror, GoogleCalendarBackend
from clinic_agent.config import load_config
from clinic_agent.db.calls import record_call
from clinic_agent.db.migrate import apply_all
from clinic_agent.db.seed import seed_from_config
from clinic_agent.reminders.worker import run_once as run_reminders
from clinic_agent.telephony.telnyx_client import TelnyxClient

log = logging.getLogger(__name__)

# How often the reminder worker scans. The claim-then-send design makes overlapping
# runs harmless, so this interval is about promptness, not safety.
REMINDER_INTERVAL_SECONDS = 900


def build_calendar_mirror(cfg):
    """Optional. Absent configuration means bookings simply are not mirrored."""
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not calendar_id or not raw:
        log.info("calendar mirror disabled (GOOGLE_CALENDAR_ID / _SERVICE_ACCOUNT_JSON unset)")
        return None
    try:
        # Accept either inline JSON or a path to the key file.
        info = json.loads(raw) if raw.strip().startswith("{") else json.loads(
            pathlib.Path(raw).read_text()
        )
        backend = GoogleCalendarBackend(info)
    except Exception:
        log.exception("calendar mirror could not start; bookings will not be mirrored")
        return None
    log.info("calendar mirror enabled for %s", calendar_id)
    return CalendarMirror(backend, calendar_id, cfg)


async def reminder_loop(pool, cfg, telnyx, from_number: str | None) -> None:
    """Scan for due reminders forever, surviving individual failures."""
    while True:
        try:
            run = await run_reminders(
                pool, cfg, telnyx=telnyx, from_number=from_number
            )
            if run.considered:
                log.info("reminder sweep: %s", run)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder sweep failed; will try again")
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)


def build_app():
    # Read .env so secrets stay in a gitignored file rather than shell history.
    # Explicit path: bare load_dotenv() searches from the caller's directory and
    # silently loads nothing when the working directory differs.
    load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

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
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD") or None,
    )
    if not deps.dashboard_password:
        log.info("dashboard disabled (DASHBOARD_PASSWORD unset)")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=10)
        # Migrations are idempotent, so applying them on boot keeps deploys simple.
        await apply_all(pool)
        await seed_from_config(pool, cfg)

        # One router serves every call: all per-call state lives on the ToolContext,
        # so there is nothing call-specific to keep here.
        mirror = build_calendar_mirror(cfg)
        sms_from = os.environ.get("TELNYX_NUMBER")

        deps.pool = pool
        deps.sms_from_number = sms_from
        deps.mirror = mirror
        deps.tool_handler = ToolRouter(
            pool=pool,
            cfg=cfg,
            secret=os.environ["SLOT_TOKEN_SECRET"],
            telnyx=telnyx,
            mirror=mirror,
        )

        async def on_finished(outcome):
            await record_call(pool, outcome)

        deps.on_call_finished = on_finished

        reminders = asyncio.create_task(
            reminder_loop(pool, cfg, telnyx, sms_from), name="reminder-loop"
        )
        log.info(
            "ready: %s, %d provider(s), reminders %s",
            cfg.clinic.name,
            len(cfg.providers),
            "dry-run" if cfg.reminders.dry_run else "live" if cfg.reminders.enabled else "off",
        )
        try:
            yield
        finally:
            reminders.cancel()
            await asyncio.gather(reminders, return_exceptions=True)
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
