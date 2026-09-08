"""Minimal operator dashboard.

Its real purpose is answering "what did the bot actually say?" -- which is the fastest
way to improve call quality -- plus seeing what is booked and whether reminders went
out. Server-rendered on purpose: no build step, no JavaScript, nothing to deploy
separately.

Everything is read-only. Nothing here can change a booking, so a leaked password
cannot cancel a patient's appointment.
"""

from __future__ import annotations

import html
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from clinic_agent.config import ClinicConfig

security = HTTPBasic()

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f6f7f9; color: #14161a; }
@media (prefers-color-scheme: dark) { body { background: #14161a; color: #e8eaed; } }
header { background: #1f2933; color: #fff; padding: 14px 22px; display: flex;
         align-items: baseline; gap: 20px; flex-wrap: wrap; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header nav a { color: #cfd8e3; text-decoration: none; margin-right: 16px; font-size: 13px; }
header nav a:hover, header nav a.on { color: #fff; text-decoration: underline; }
main { padding: 22px; max-width: 1100px; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
     color: #6b7280; margin: 26px 0 10px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,.08); }
@media (prefers-color-scheme: dark) { table { background: #1e2126; } }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid rgba(128,128,128,.18);
         vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; }
tr:last-child td { border-bottom: none; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
       font-weight: 600; }
.ok { background: #dcfce7; color: #166534; }
.warn { background: #fef3c7; color: #92400e; }
.bad { background: #fee2e2; color: #991b1b; }
.mute { background: rgba(128,128,128,.18); color: #6b7280; }
.empty { padding: 26px; text-align: center; color: #6b7280; }
.transcript { margin: 0; font-size: 13px; }
.transcript div { margin: 2px 0; }
.who { display: inline-block; width: 52px; color: #6b7280; font-size: 12px; }
.banner { background: #fef3c7; color: #92400e; padding: 9px 22px; font-size: 13px; }
"""

STATUS_CLASS = {
    "sent": "ok", "booked": "ok", "telnyx_stop": "ok", "opted_in": "ok",
    "dry_run": "warn", "claimed": "warn", "pending": "warn", "reconnect_limit": "warn",
    "failed": "bad", "unsupported_codec": "bad",
    "cancelled": "mute", "skipped_opted_out": "mute", "no_show": "mute",
}


def tag(value: Any) -> str:
    text = html.escape(str(value or "-"))
    return f'<span class="tag {STATUS_CLASS.get(str(value), "mute")}">{text}</span>'


def page(title: str, active: str, cfg: ClinicConfig, body: str) -> HTMLResponse:
    banner = ""
    if not cfg.reminders.enabled:
        banner = (
            '<div class="banner">Reminders are switched off in config, so the agent does '
            "not promise them. Turn on <code>reminders.enabled</code> once 10DLC "
            "registration is approved.</div>"
        )
    elif cfg.reminders.dry_run:
        banner = (
            '<div class="banner">Reminders are in <strong>dry-run</strong>: bodies are '
            "recorded, nothing is sent.</div>"
        )

    def link(href: str, label: str, key: str) -> str:
        return f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} - {html.escape(cfg.clinic.name)}</title>
<style>{STYLE}</style></head><body>
<header>
  <h1>{html.escape(cfg.clinic.name)}</h1>
  <nav>
    {link("/dashboard", "Calls", "calls")}
    {link("/dashboard/appointments", "Appointments", "appointments")}
    {link("/dashboard/reminders", "Reminders", "reminders")}
  </nav>
</header>{banner}
<main>{body}</main></body></html>""")


def local(moment: datetime | None, cfg: ClinicConfig) -> str:
    if moment is None:
        return "-"
    return moment.astimezone(cfg.tz).strftime("%a %d %b, %H:%M")


def table(headers: list[str], rows: list[str], empty: str) -> str:
    if not rows:
        return f'<table><tr><td class="empty">{html.escape(empty)}</td></tr></table>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_transcript(raw: Any) -> str:
    try:
        entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return "<em>unreadable</em>"
    if not entries:
        return '<span class="mute">no transcript</span>'
    lines = "".join(
        f'<div><span class="who">{html.escape(str(e.get("role", "?")))}</span>'
        f'{html.escape(str(e.get("text", "")))}</div>'
        for e in entries
    )
    return f'<div class="transcript">{lines}</div>'


def build_dashboard(cfg: ClinicConfig, pool_getter, password: str) -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def authorise(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        # compare_digest so a wrong password cannot be discovered by timing.
        ok = secrets.compare_digest(credentials.password, password)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not authorised",
                headers={"WWW-Authenticate": "Basic"},
            )

    @router.get("", response_class=HTMLResponse)
    async def calls(_=Depends(authorise)) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return page("Calls", "calls", cfg, "<p>No database configured.</p>")
        rows = await pool.fetch(
            """
            SELECT from_number, started_at, ended_at, outcome, transcript
            FROM calls ORDER BY started_at DESC LIMIT 40
            """
        )
        body = table(
            ["From", "Started", "Outcome", "Transcript"],
            [
                "<tr>"
                f"<td>{html.escape(str(r['from_number'] or '-'))}</td>"
                f"<td>{local(r['started_at'], cfg)}</td>"
                f"<td>{tag(r['outcome'])}</td>"
                f"<td>{render_transcript(r['transcript'])}</td>"
                "</tr>"
                for r in rows
            ],
            "No calls yet.",
        )
        return page("Calls", "calls", cfg, f"<h2>Recent calls</h2>{body}")

    @router.get("/appointments", response_class=HTMLResponse)
    async def appointments(_=Depends(authorise)) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return page("Appointments", "appointments", cfg, "<p>No database configured.</p>")
        rows = await pool.fetch(
            """
            SELECT a.starts_at, a.status::text AS status, a.source,
                   pt.name AS patient, pt.phone,
                   p.name AS provider, t.name AS type
            FROM appointments a
            JOIN patients pt ON pt.id = a.patient_id
            JOIN providers p ON p.id = a.provider_id
            JOIN appointment_types t ON t.id = a.appointment_type_id
            WHERE a.starts_at > $1
            ORDER BY a.starts_at
            LIMIT 100
            """,
            datetime.now(UTC) - timedelta(hours=2),
        )
        body = table(
            ["When", "Patient", "Phone", "Provider", "Type", "Status", "Source"],
            [
                "<tr>"
                f"<td>{local(r['starts_at'], cfg)}</td>"
                f"<td>{html.escape(r['patient'])}</td>"
                f"<td>{html.escape(r['phone'])}</td>"
                f"<td>{html.escape(r['provider'])}</td>"
                f"<td>{html.escape(r['type'])}</td>"
                f"<td>{tag(r['status'])}</td>"
                f"<td>{html.escape(r['source'])}</td>"
                "</tr>"
                for r in rows
            ],
            "Nothing booked.",
        )
        return page("Appointments", "appointments", cfg, f"<h2>Upcoming</h2>{body}")

    @router.get("/reminders", response_class=HTMLResponse)
    async def reminders(_=Depends(authorise)) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return page("Reminders", "reminders", cfg, "<p>No database configured.</p>")
        rows = await pool.fetch(
            """
            SELECT r.status, r.sent_at, r.attempts, r.body, r.last_error,
                   a.starts_at, pt.phone
            FROM reminders r
            JOIN appointments a ON a.id = r.appointment_id
            JOIN patients pt ON pt.id = a.patient_id
            ORDER BY r.updated_at DESC LIMIT 60
            """
        )
        body = table(
            ["Appointment", "Phone", "Status", "Tries", "Message", "Error"],
            [
                "<tr>"
                f"<td>{local(r['starts_at'], cfg)}</td>"
                f"<td>{html.escape(r['phone'])}</td>"
                f"<td>{tag(r['status'])}</td>"
                f"<td>{r['attempts']}</td>"
                f"<td>{html.escape(r['body'] or '-')}</td>"
                f"<td>{html.escape(r['last_error'] or '')}</td>"
                "</tr>"
                for r in rows
            ],
            "No reminders yet.",
        )
        return page("Reminders", "reminders", cfg, f"<h2>Reminder log</h2>{body}")

    return router
