"""The 'Task Titan Handyman' console section: the third agent's tab.

Its own lightweight shell (reusing the shared design system and the mic test console), so
the clinic and trailer consoles are untouched. Auth is the same signed-cookie session.
Four pages: the voice assistant, appointment requests, call transcriptions, and a combined
leads-and-messages inbox. Read-only except the status/mark actions.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse

from clinic_agent.bridge.call_session import CallSession
from clinic_agent.db.handyman_calls import record_handyman_call
from clinic_agent.handyman_config import HandymanConfig
from clinic_agent.web import theme
from clinic_agent.web.auth import SESSION_COOKIE, read_form, require_session, verify_token
from clinic_agent.web.dashboard import (
    _badge,
    _BrowserSocket,
    render_test_console_body,
    render_transcript,
)
from clinic_agent.web.theme import CSS, empty_state, icon

log = logging.getLogger(__name__)

_NAV = [
    ("voice", "/dashboard/handyman/voice", "Voice assistant", "mic"),
    ("appointments", "/dashboard/handyman/appointments", "Appointments", "calendar"),
    ("transcriptions", "/dashboard/handyman/transcriptions", "Transcriptions", "phone"),
    ("leads", "/dashboard/handyman/leads", "Leads & Messages", "inbox"),
]

APPT_STATUS_CLASS = {"requested": "warn", "confirmed": "ok", "done": "mute", "declined": "bad"}
LEAD_STATUS_CLASS = {"new": "warn", "contacted": "ok", "closed": "mute"}
MSG_STATUS_CLASS = {"new": "warn", "read": "mute"}
# The status a caller-facing action moves a row to.
NEXT_APPT_STATUS = {"requested", "confirmed", "done", "declined"}


def _shell(cfg: HandymanConfig, active: str, body: str, *, lead: str = "", full_bleed: bool = False) -> str:
    name = html.escape(cfg.business.name)
    links = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{icon(ic)}'
        f'<span class="t">{label}</span></a>'
        for key, href, label, ic in _NAV
    )
    content_cls = "content" + (" full" if full_bleed else "")
    lead_html = f'<span class="lead">{html.escape(lead)}</span>' if lead else ""
    title = active.replace("_", " ").title()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)} · {name}</title><style>{CSS}</style></head>
<body><div class="app">
  <aside class="rail">
    <div class="brand"><div class="mark">{icon("mic")}</div>
      <div><div class="name">{name}</div><div class="sub">Handyman assistant</div></div></div>
    <nav class="nav" aria-label="Sections">{links}</nav>
    <div class="rail-foot">
      <form method="post" action="/logout"><button class="logout" type="submit">{icon("logout")}<span>Sign out</span></button></form>
    </div>
  </aside>
  <div class="main"><div class="topbar"><h1>{html.escape(title)}</h1>{lead_html}</div>
    <div class="{content_cls}">{body}</div></div>
</div></body></html>"""


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _section(title: str, inner: str) -> str:
    return f'<div class="section-head"><h2>{html.escape(title)}</h2></div>{inner}'


def _local(moment: datetime | None, cfg: HandymanConfig) -> str:
    if moment is None:
        return "-"
    return moment.astimezone(cfg.tz).strftime("%a %d %b, %H:%M")


def _status_form(action: str, status: str, label: str, *, primary: bool = False) -> str:
    cls = "btn btn-sm" + (" btn-primary" if primary else "")
    return (
        f'<form method="post" action="{action}" style="display:inline">'
        f'<input type="hidden" name="status" value="{status}">'
        f'<button class="{cls}" type="submit">{html.escape(label)}</button></form>'
    )


def build_handyman_dashboard(
    cfg: HandymanConfig,
    pool_getter,
    password: str,
    *,
    connect_gemini=None,
    tool_handler_getter=None,
) -> APIRouter:
    router = APIRouter(prefix="/dashboard/handyman", tags=["handyman"])
    guard = Depends(require_session(password))

    # --- voice assistant ------------------------------------------------------

    @router.get("/voice", response_class=HTMLResponse)
    async def voice_page(_=guard) -> HTMLResponse:
        if connect_gemini is None:
            body = f'<div class="card">{empty_state("mic", "Voice agent not configured", "Set GEMINI_API_KEY so the handyman agent can connect.")}</div>'
            return HTMLResponse(_shell(cfg, "voice", body, lead="Talk to the handyman assistant"))
        body = render_test_console_body(
            ws_path="/dashboard/handyman/voicecall",
            hint="Talk to the handyman assistant live through your microphone — the same engine "
                 "that would answer calls. Nothing is dialed. Works best in Chrome.",
        )
        return HTMLResponse(_shell(cfg, "voice", body, lead="Talk to the handyman assistant", full_bleed=True))

    @router.websocket("/voicecall")
    async def voice_call(websocket: WebSocket) -> None:
        if not verify_token(websocket.cookies.get(SESSION_COOKIE), password):
            await websocket.close(code=1008)
            return
        if connect_gemini is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()

        async def sink(role: str, text: str) -> None:
            try:
                await websocket.send_text(json.dumps({"event": "transcript", "role": role, "text": text}))
            except Exception:
                log.debug("could not forward a handyman transcript fragment", exc_info=True)

        session = CallSession(
            telnyx=_BrowserSocket(websocket),
            connect_gemini=connect_gemini,
            tool_handler=tool_handler_getter() if tool_handler_getter else None,
            on_transcript=sink,
            drain_timeout=1.5,
        )
        try:
            outcome = await session.run()
        except Exception:
            log.exception("browser handyman test call failed")
            try:
                await websocket.send_text(json.dumps({"event": "error",
                    "message": "The AI session ended unexpectedly. Check the server logs."}))
            except Exception:
                log.debug("could not send handyman error frame", exc_info=True)
            return
        # Persist the console session so the Transcriptions page has data before a real
        # phone line is wired. Failures here never affect the call, which is already over.
        pool = pool_getter()
        if pool is not None:
            await record_handyman_call(pool, outcome, source="console")
        if outcome.ended_reason == "reconnect_limit":
            try:
                await websocket.send_text(json.dumps({"event": "error",
                    "message": "Couldn't connect to the AI — check GEMINI_API_KEY."}))
            except Exception:
                log.debug("could not send handyman error frame", exc_info=True)

    # --- appointment requests -------------------------------------------------

    @router.get("/appointments", response_class=HTMLResponse)
    async def appointments(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return HTMLResponse(_shell(cfg, "appointments", '<div class="card">No database.</div>'))
        rows = await pool.fetch(
            """
            SELECT id, name, phone, email, job_type, description, address, preferred_time,
                   status::text AS status, created_at
            FROM handyman_appointment_requests
            ORDER BY created_at DESC LIMIT 200
            """
        )
        if not rows:
            body = empty_state("calendar", "No requests yet",
                               "Visit and estimate requests the assistant takes will appear here.")
        else:
            trs = []
            for r in rows:
                job = html.escape(r["job_type"] or "-")
                desc = html.escape(r["description"] or "")
                job_cell = f"{job}" + (f'<div class="subtle">{desc}</div>' if desc else "")
                actions = (
                    _status_form(f"/dashboard/handyman/appointments/{r['id']}/status", "confirmed", "Confirm", primary=True)
                    + " "
                    + _status_form(f"/dashboard/handyman/appointments/{r['id']}/status", "done", "Done")
                    + " "
                    + _status_form(f"/dashboard/handyman/appointments/{r['id']}/status", "declined", "Decline")
                )
                trs.append(
                    "<tr>"
                    f'<td class="mono subtle">{_local(r["created_at"], cfg)}</td>'
                    f"<td>{html.escape(r['name'] or '-')}</td>"
                    f'<td class="mono subtle">{html.escape(r["phone"])}</td>'
                    f'<td class="subtle">{html.escape(r["email"] or "-")}</td>'
                    f"<td>{job_cell}</td>"
                    f"<td>{html.escape(r['address'] or '-')}</td>"
                    f"<td>{html.escape(r['preferred_time'] or '-')}</td>"
                    f"<td>{theme.tag(r['status'], APPT_STATUS_CLASS.get(r['status'], 'mute'))}</td>"
                    f'<td class="nowrap">{actions}</td>'
                    "</tr>"
                )
            body = _table(
                ["Taken", "Name", "Phone", "Email", "Job", "Where", "Preferred", "Status", ""], trs
            )
        return HTMLResponse(_shell(cfg, "appointments", body, lead="Visit & estimate requests"))

    @router.post("/appointments/{request_id}/status")
    async def set_appointment_status(request_id: int, request: Request, _=guard):
        form = await read_form(request)
        status = str(form.get("status") or "")
        if status in NEXT_APPT_STATUS:
            pool = pool_getter()
            if pool is not None:
                await pool.execute(
                    "UPDATE handyman_appointment_requests SET status=$1::handyman_appt_status,"
                    " updated_at=now() WHERE id=$2",
                    status, request_id,
                )
        return RedirectResponse("/dashboard/handyman/appointments", status_code=303)

    # --- transcriptions -------------------------------------------------------

    @router.get("/transcriptions", response_class=HTMLResponse)
    async def transcriptions(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return HTMLResponse(_shell(cfg, "transcriptions", '<div class="card">No database.</div>'))
        rows = await pool.fetch(
            """
            SELECT from_number, source, started_at, outcome, transcript
            FROM handyman_calls ORDER BY started_at DESC LIMIT 100
            """
        )
        if not rows:
            body = empty_state("phone", "No calls yet",
                               "When the assistant answers a call, the transcript and outcome show up here.")
        else:
            trs = [
                "<tr>"
                f'<td class="mono subtle">{_local(r["started_at"], cfg)}</td>'
                f"<td>{html.escape(r['from_number'] or ('Test console' if r['source'] == 'console' else '-'))}</td>"
                f"<td>{_badge(r['outcome'])}</td>"
                f"<td>{render_transcript(r['transcript'])}</td>"
                "</tr>"
                for r in rows
            ]
            body = _table(["When", "From", "Outcome", "Transcript"], trs)
        return HTMLResponse(_shell(cfg, "transcriptions", body, lead="One row per call"))

    # --- leads & messages -----------------------------------------------------

    @router.get("/leads", response_class=HTMLResponse)
    async def leads(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return HTMLResponse(_shell(cfg, "leads", '<div class="card">No database.</div>'))
        lead_rows = await pool.fetch(
            "SELECT id, name, phone, reason, status::text AS status, created_at"
            " FROM handyman_leads ORDER BY created_at DESC LIMIT 200"
        )
        msg_rows = await pool.fetch(
            "SELECT id, caller_name, phone, message, status::text AS status, created_at"
            " FROM handyman_messages ORDER BY created_at DESC LIMIT 200"
        )

        if lead_rows:
            trs = [
                "<tr>"
                f'<td class="mono subtle">{_local(r["created_at"], cfg)}</td>'
                f"<td>{html.escape(r['name'] or '-')}</td>"
                f'<td class="mono subtle">{html.escape(r["phone"])}</td>'
                f"<td>{html.escape(r['reason'] or '-')}</td>"
                f"<td>{theme.tag(r['status'], LEAD_STATUS_CLASS.get(r['status'], 'mute'))}</td>"
                f'<td class="nowrap">'
                f'<form method="post" action="/dashboard/handyman/leads/{r["id"]}/contacted" style="display:inline">'
                f'<button class="btn btn-sm btn-primary" type="submit">Mark contacted</button></form></td>'
                "</tr>"
                for r in lead_rows
            ]
            leads_html = _table(["Taken", "Name", "Phone", "About", "Status", ""], trs)
        else:
            leads_html = empty_state("inbox", "No callback leads",
                                     "Callback requests the assistant takes will appear here.")

        if msg_rows:
            trs = [
                "<tr>"
                f'<td class="mono subtle">{_local(r["created_at"], cfg)}</td>'
                f"<td>{html.escape(r['caller_name'] or '-')}</td>"
                f'<td class="mono subtle">{html.escape(r["phone"] or "-")}</td>'
                f"<td>{html.escape(r['message'])}</td>"
                f"<td>{theme.tag(r['status'], MSG_STATUS_CLASS.get(r['status'], 'mute'))}</td>"
                f'<td class="nowrap">'
                f'<form method="post" action="/dashboard/handyman/messages/{r["id"]}/read" style="display:inline">'
                f'<button class="btn btn-sm" type="submit">Mark read</button></form></td>'
                "</tr>"
                for r in msg_rows
            ]
            msgs_html = _table(["Taken", "From", "Phone", "Message", "Status", ""], trs)
        else:
            msgs_html = empty_state("inbox", "No messages",
                                    "Personal messages for the owner will appear here.")

        body = _section("Callback leads", leads_html) + _section("Personal messages", msgs_html)
        return HTMLResponse(_shell(cfg, "leads", body, lead="Callbacks & personal messages"))

    @router.post("/leads/{lead_id}/contacted")
    async def mark_lead_contacted(lead_id: int, _=guard):
        pool = pool_getter()
        if pool is not None:
            await pool.execute(
                "UPDATE handyman_leads SET status='contacted', updated_at=now() WHERE id=$1",
                lead_id,
            )
        return RedirectResponse("/dashboard/handyman/leads", status_code=303)

    @router.post("/messages/{message_id}/read")
    async def mark_message_read(message_id: int, _=guard):
        pool = pool_getter()
        if pool is not None:
            await pool.execute(
                "UPDATE handyman_messages SET status='read' WHERE id=$1", message_id
            )
        return RedirectResponse("/dashboard/handyman/leads", status_code=303)

    return router
