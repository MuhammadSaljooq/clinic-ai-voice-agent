"""The 'Trailer rental' console section: a separate tab for the second agent.

Its own lightweight shell (reusing the shared design system and the mic test console),
so the clinic console is untouched. Auth is the same signed-cookie session.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, WebSocket
from fastapi.responses import HTMLResponse

from clinic_agent.bridge.call_session import CallSession
from clinic_agent.rental_config import RentalConfig
from clinic_agent.web import theme
from clinic_agent.web.auth import SESSION_COOKIE, require_session, verify_token
from clinic_agent.web.dashboard import _BrowserSocket, render_test_console_body
from clinic_agent.web.theme import CSS, empty_state, icon

log = logging.getLogger(__name__)

_NAV = [
    ("test", "/dashboard/trailer/test", "Test agent", "mic"),
    ("inventory", "/dashboard/trailer/inventory", "Inventory", "calendar"),
    ("rentals", "/dashboard/trailer/rentals", "Rentals", "inbox"),
]

STATUS_CLASS = {"booked": "ok", "completed": "mute", "cancelled": "bad"}


def _shell(cfg: RentalConfig, active: str, body: str, *, lead: str = "", full_bleed: bool = False) -> str:
    name = html.escape(cfg.business.name)
    links = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{icon(ic)}'
        f'<span class="t">{label}</span></a>'
        for key, href, label, ic in _NAV
    )
    content_cls = "content" + (" full" if full_bleed else "")
    lead_html = f'<span class="lead">{html.escape(lead)}</span>' if lead else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(active.title())} · {name}</title><style>{CSS}</style></head>
<body><div class="app">
  <aside class="rail">
    <div class="brand"><div class="mark">{icon("calendar")}</div>
      <div><div class="name">{name}</div><div class="sub">Trailer rentals</div></div></div>
    <nav class="nav" aria-label="Sections">{links}</nav>
    <div class="rail-foot">
      <a class="logout" href="/dashboard/inbox" style="text-decoration:none">{icon("logout")}<span>Clinic console</span></a>
      <form method="post" action="/logout"><button class="logout" type="submit">{icon("logout")}<span>Sign out</span></button></form>
    </div>
  </aside>
  <div class="main"><div class="topbar"><h1>{html.escape(active.title())}</h1>{lead_html}</div>
    <div class="{content_cls}">{body}</div></div>
</div></body></html>"""


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def build_rental_dashboard(
    cfg: RentalConfig,
    pool_getter,
    password: str,
    *,
    connect_gemini=None,
    tool_handler_getter=None,
) -> APIRouter:
    router = APIRouter(prefix="/dashboard/trailer", tags=["trailer"])
    guard = Depends(require_session(password))

    def _local(d: date | datetime | None) -> str:
        if d is None:
            return "-"
        return d.strftime("%a %d %b")

    @router.get("/test", response_class=HTMLResponse)
    async def test_page(_=guard) -> HTMLResponse:
        if connect_gemini is None:
            body = f'<div class="card">{empty_state("mic", "Voice agent not configured", "Set GEMINI_API_KEY so the trailer agent can connect.")}</div>'
            return HTMLResponse(_shell(cfg, "test", body, lead="Talk to the trailer agent"))
        body = render_test_console_body(
            ws_path="/dashboard/trailer/testcall",
            hint="Talk to the trailer-rental agent live through your microphone — the same engine "
                 "that would answer calls. Nothing is dialed. Works best in Chrome.",
        )
        return HTMLResponse(_shell(cfg, "test", body, lead="Talk to the trailer agent", full_bleed=True))

    @router.websocket("/testcall")
    async def test_call(websocket: WebSocket) -> None:
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
                log.debug("could not forward a rental transcript fragment", exc_info=True)

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
            log.exception("browser trailer test call failed")
            with_error = "The AI session ended unexpectedly. Check the server logs."
            try:
                await websocket.send_text(json.dumps({"event": "error", "message": with_error}))
            except Exception:
                log.debug("could not send trailer error frame", exc_info=True)
            return
        if outcome.ended_reason == "reconnect_limit":
            try:
                await websocket.send_text(json.dumps({"event": "error",
                    "message": "Couldn't connect to the AI — check GEMINI_API_KEY."}))
            except Exception:
                log.debug("could not send trailer error frame", exc_info=True)

    @router.get("/inventory", response_class=HTMLResponse)
    async def inventory(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return HTMLResponse(_shell(cfg, "inventory", '<div class="card">No database.</div>'))
        rows = await pool.fetch(
            """
            SELECT tt.name, tt.daily_rate, tt.deposit, count(u.id) AS units
            FROM trailer_types tt LEFT JOIN trailer_units u ON u.trailer_type_id = tt.id
            WHERE tt.active GROUP BY tt.id, tt.name, tt.daily_rate, tt.deposit ORDER BY tt.name
            """
        )
        trs = [
            "<tr>"
            f"<td>{html.escape(r['name'])}</td>"
            f'<td class="num mono">${r["daily_rate"]:.2f}</td>'
            f'<td class="num mono">${r["deposit"]:.2f}</td>'
            f'<td class="num mono">{r["units"]}</td>'
            "</tr>"
            for r in rows
        ]
        body = (_table(["Trailer", "Daily rate", "Deposit", "Units"], trs)
                if trs else empty_state("calendar", "No trailers", "Seed the trailer config to see inventory."))
        return HTMLResponse(_shell(cfg, "inventory", body, lead="Trailer types and units"))

    @router.get("/rentals", response_class=HTMLResponse)
    async def rentals(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return HTMLResponse(_shell(cfg, "rentals", '<div class="card">No database.</div>'))
        rows = await pool.fetch(
            """
            SELECT r.id, r.pickup_date, r.return_date, r.total_cost, r.deposit, r.status::text AS status,
                   tt.name AS trailer, c.name AS customer, c.phone
            FROM rentals r
            JOIN trailer_units u ON u.id = r.trailer_unit_id
            JOIN trailer_types tt ON tt.id = u.trailer_type_id
            JOIN customers c ON c.id = r.customer_id
            WHERE r.return_date >= $1
            ORDER BY r.pickup_date LIMIT 100
            """,
            datetime.now(UTC).date(),
        )
        if not rows:
            body = empty_state("inbox", "No rentals", "Booked rentals will appear here.")
        else:
            trs = [
                "<tr>"
                f'<td class="mono">{_local(r["pickup_date"])} → {_local(r["return_date"])}</td>'
                f"<td>{html.escape(r['trailer'])}</td>"
                f"<td>{html.escape(r['customer'])}</td>"
                f'<td class="mono subtle">{html.escape(r["phone"])}</td>'
                f'<td class="num mono">${r["total_cost"]:.2f}</td>'
                f"<td>{theme.tag(r['status'], STATUS_CLASS.get(r['status'], 'mute'))}</td>"
                "</tr>"
                for r in rows
            ]
            body = _table(["When", "Trailer", "Customer", "Phone", "Total", "Status"], trs)
        return HTMLResponse(_shell(cfg, "rentals", body, lead="Upcoming and active rentals"))

    return router
