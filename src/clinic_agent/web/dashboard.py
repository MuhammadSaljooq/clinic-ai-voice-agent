"""Operator console: calls, appointments, reminders, and a two-way SMS inbox.

Its jobs, in order of how often they are used: reply to patients (the inbox), see what
the bot actually said on a call, see what is booked, and confirm reminders went out.

Server-rendered on purpose -- no build step, no separate deploy. Authentication is a
signed-cookie session (see `web.auth`); every page depends on it. The only state this
surface can change is sending an SMS, which is a deliberate, opt-out-respecting operator
action. It still cannot touch a booking, so a leaked session cannot cancel a patient's
appointment.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from clinic_agent.bridge.call_session import CallSession
from clinic_agent.config import ClinicConfig
from clinic_agent.messaging import outbound, store
from clinic_agent.web.auth import SESSION_COOKIE, read_form, require_session, verify_token
from clinic_agent.web.theme import empty_state, icon, local, shell, tag

log = logging.getLogger(__name__)

# Map a raw status/outcome string to a badge colour class.
STATUS_CLASS = {
    "sent": "ok", "delivered": "ok", "booked": "ok", "telnyx_stop": "ok", "opted_in": "ok",
    "received": "info", "queued": "info",
    "dry_run": "warn", "claimed": "warn", "sending": "warn", "pending": "warn",
    "reconnect_limit": "warn",
    "failed": "bad", "blocked": "bad", "unsupported_codec": "bad",
    "cancelled": "mute", "skipped_opted_out": "mute", "no_show": "mute", "completed": "mute",
}


def _badge(value: Any) -> str:
    return tag(value, STATUS_CLASS.get(str(value), "mute"))


def render_transcript(raw: Any) -> str:
    try:
        entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return "<em>unreadable</em>"
    if not entries:
        return '<span class="subtle">no transcript</span>'
    rows = []
    for e in entries:
        role = str(e.get("role", "?"))
        cls = "agent" if role == "agent" else "caller"
        rows.append(
            f'<div class="{cls}"><span class="who">{html.escape(role)}</span>'
            f'{html.escape(str(e.get("text", "")))}</div>'
        )
    return f'<div class="transcript">{"".join(rows)}</div>'


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _initials(name: str | None, phone: str) -> str:
    if name and name.strip() and not name.startswith("("):
        parts = [p for p in name.split() if p]
        return html.escape((parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")).upper())
    return html.escape(phone[-2:] if len(phone) >= 2 else "#")


def _preview(body: str, direction: str) -> str:
    text = html.escape(body[:80])
    if direction == "outbound":
        return f'<span class="you">You:</span> {text}'
    return text


# Browser audio engine for the test console. Talks the same Telnyx frame protocol the
# phone path uses: mic -> 16 kHz PCM16 big-endian ("L16") -> media frames; agent audio
# comes back as media frames and is played, with `clear` flushing playback on barge-in.
# Forced to a 16 kHz AudioContext so no resampling is needed either way. Best in Chrome.
_TEST_JS = r"""
const MIC = `__MIC__`, STOP = `__STOP__`;
// Two worklets in one module: 'cap' posts captured mic frames to the main thread;
// 'play' is a pull-based jitter buffer -- the audio graph pulls 128 samples every
// quantum from a ring buffer we fill as frames arrive, so network/arrival wobble never
// turns into gaps or clicks. It waits until ~120ms is buffered (PRIME) before draining,
// and outputs silence (not a click) on underrun, then re-primes.
const WORKLET = `
class Cap extends AudioWorkletProcessor {
  process(i){ const c=i[0][0]; if(c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor('cap', Cap);
class Play extends AudioWorkletProcessor {
  constructor(){ super(); this.buf=new Float32Array(16000*6); this.r=0; this.w=0; this.n=0;
    this.PRIME=960; this.primed=false;   // 60ms lead buffer (lower = snappier, riskier)
    this.port.onmessage=e=>{ const d=e.data;
      if(d==='clear'){ this.r=this.w=this.n=0; this.primed=false; return; }
      for(let i=0;i<d.length;i++){ this.buf[this.w]=d[i]; this.w=(this.w+1)%this.buf.length;
        if(this.n<this.buf.length) this.n++; else this.r=(this.r+1)%this.buf.length; } };
  }
  process(_i, outputs){ const o=outputs[0][0];
    if(!this.primed){ if(this.n>=this.PRIME) this.primed=true; else { o.fill(0); return true; } }
    for(let i=0;i<o.length;i++){ if(this.n>0){ o[i]=this.buf[this.r]; this.r=(this.r+1)%this.buf.length; this.n--; } else { o[i]=0; this.primed=false; } }
    return true;
  }
}
registerProcessor('play', Play);
`;
let ctx, stream, capNode, playNode, ws, running = false, chunkNo = 0, acc = new Float32Array(0);
let playbackUntil = 0;
const btn = document.getElementById('talk'), statusEl = document.getElementById('tstatus');
const level = document.getElementById('level-fill'), streamEl = document.getElementById('stream');
const setStatus = t => statusEl.textContent = t;
const b64 = b => { let s=''; for (let i=0;i<b.length;i++) s+=String.fromCharCode(b[i]); return btoa(s); };
const unb64 = s => { const x=atob(s), b=new Uint8Array(x.length); for (let i=0;i<x.length;i++) b[i]=x.charCodeAt(i); return b; };

function addLine(role, text){
  const last = streamEl.lastElementChild;
  if (last && last.dataset.role === role) { last.querySelector('.t').textContent += text; }
  else {
    const d = document.createElement('div');
    d.className = 'bubble ' + (role === 'agent' ? 'out' : 'in'); d.dataset.role = role;
    d.innerHTML = '<span class="t"></span>'; d.querySelector('.t').textContent = text;
    streamEl.appendChild(d);
  }
  streamEl.scrollTop = streamEl.scrollHeight;
}
function pushAudio(bytes){
  const n = Math.floor(bytes.length/2); if (!n || !playNode) return;
  const f = new Float32Array(n);
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i=0;i<n;i++) f[i] = dv.getInt16(i*2, false) / 32768;  // big-endian L16
  playNode.port.postMessage(f, [f.buffer]);
  // Track how long we'll be playing, so the mic uplink can be muted meanwhile
  // (browser echo-cancellation misses Web Audio output, so without this the agent
  // hears itself and replies to its own voice).
  const start = Math.max(playbackUntil, performance.now());
  playbackUntil = start + (n / 16000) * 1000;
}
const flush = () => { if (playNode) playNode.port.postMessage('clear'); };

async function start(){
  setStatus('Requesting microphone…'); btn.classList.add('live'); btn.innerHTML = STOP;
  try { stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:false}}); }
  catch(e){ setStatus('Microphone permission is required.'); btn.classList.remove('live'); btn.innerHTML = MIC; return; }
  ctx = new (window.AudioContext || window.webkitAudioContext)({sampleRate:16000});
  await ctx.resume();
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], {type:'application/javascript'})));
  capNode = new AudioWorkletNode(ctx, 'cap'); ctx.createMediaStreamSource(stream).connect(capNode);
  playNode = new AudioWorkletNode(ctx, 'play'); playNode.connect(ctx.destination);
  running = true; chunkNo = 0; acc = new Float32Array(0);
  setStatus('Connecting…');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '__WS_PATH__');
  ws.onopen = () => { setStatus('Listening — say hello'); ws.send(JSON.stringify({event:'start', stream_id:'browser', start:{call_control_id:'browser-test', call_session_id:'bt', from:'browser-tester', to:'console', media_format:{encoding:'L16', sample_rate:16000, channels:1}}})); };
  ws.onmessage = ev => { let m; try { m = JSON.parse(ev.data); } catch(e){ return; }
    if (m.event === 'media' && m.media && m.media.payload) pushAudio(unb64(m.media.payload));
    else if (m.event === 'clear') flush();
    else if (m.event === 'transcript') addLine(m.role, m.text);
    else if (m.event === 'error') { setStatus(m.message); running = false; setTimeout(stop, 50); } };
  ws.onclose = () => { if (running) stop(); };
  capNode.port.onmessage = e => {
    if (!running || !ws || ws.readyState !== 1) return;
    const inc = e.data; let sum = 0; for (let i=0;i<inc.length;i++) sum += inc[i]*inc[i];
    level.style.width = Math.min(100, Math.sqrt(sum/inc.length) * 320) + '%';
    // Half-duplex echo guard: while the agent is playing, don't uplink mic audio, so it
    // can't hear itself (browser echo-cancellation misses Web Audio output). Barge-in is
    // off, so nothing is lost.
    if (performance.now() < playbackUntil + 200) { acc = new Float32Array(0); return; }
    const merged = new Float32Array(acc.length + inc.length); merged.set(acc); merged.set(inc, acc.length); acc = merged;
    while (acc.length >= 320) {
      const frame = acc.subarray(0, 320); acc = acc.slice(320);
      const bytes = new Uint8Array(640), dv = new DataView(bytes.buffer);
      for (let i=0;i<320;i++){ let v = Math.max(-1, Math.min(1, frame[i])); dv.setInt16(i*2, v<0 ? v*32768 : v*32767, false); }
      ws.send(JSON.stringify({event:'media', stream_id:'browser', media:{track:'inbound', chunk:String(++chunkNo), timestamp:String(chunkNo*20), payload:b64(bytes)}}));
    }
  };
}
function stop(){
  running = false;
  try { if (ws && ws.readyState === 1) ws.send(JSON.stringify({event:'stop', stop:{call_control_id:'browser-test'}})); } catch(e){}
  try { ws && ws.close(); } catch(e){}
  try { capNode && capNode.disconnect(); } catch(e){}
  try { playNode && playNode.disconnect(); } catch(e){}
  try { stream && stream.getTracks().forEach(t => t.stop()); } catch(e){}
  try { ctx && ctx.close(); } catch(e){}
  playbackUntil = 0;
  acc = new Float32Array(0); btn.classList.remove('live'); btn.innerHTML = MIC;
  setStatus('Tap to talk to the agent'); level.style.width = '0%';
}
btn.addEventListener('click', () => { running ? stop() : start(); });
window.addEventListener('beforeunload', () => { if (running) stop(); });
"""


def render_test_console_body(*, ws_path: str, hint: str) -> str:
    """The mic test-console body (panel + conversation + engine JS), reusable by any
    agent. `ws_path` is the WebSocket route to run the CallSession on."""
    js = (_TEST_JS
          .replace("__MIC__", icon("mic"))
          .replace("__STOP__", icon("stop"))
          .replace("__WS_PATH__", ws_path))
    return f"""<div class="test">
      <div class="card test-panel">
        <button id="talk" class="talk-btn" type="button" aria-label="Start talking to the agent">{icon("mic")}</button>
        <div class="test-status" id="tstatus">Tap to talk to the agent</div>
        <div class="level" aria-hidden="true"><i id="level-fill"></i></div>
        <p class="test-hint">{html.escape(hint)}</p>
      </div>
      <div class="card convo test-convo">
        <div class="convo-head"><div class="avatar">AI</div>
          <div><b>Live test</b><div class="num">what you and the agent say appears here</div></div>
        </div>
        <div class="stream" id="stream"></div>
      </div>
    </div>
    <script>{js}</script>"""


def render_test_page(cfg: ClinicConfig, *, enabled: bool, inbox_count: int) -> str:
    if not enabled:
        body = empty_state(
            "mic", "Voice agent not configured",
            "Set GEMINI_API_KEY (or Vertex) so the agent can connect, then reload to talk to it.",
        )
        return shell("Test agent", "test", cfg, f'<div class="card">{body}</div>',
                     lead="Talk to the AI", inbox_count=inbox_count)

    body = render_test_console_body(
        ws_path="/dashboard/testcall",
        hint="Talk to the AI receptionist live through your microphone — the same engine that "
             "answers calls. Nothing is dialed. Works best in Chrome; allow mic access when asked.",
    )
    return shell("Test agent", "test", cfg, body, lead="Talk to the AI",
                 inbox_count=inbox_count, full_bleed=True)


class _BrowserSocket:
    """Adapts a FastAPI WebSocket to the send/async-iterate shape CallSession expects,
    so the browser test console reuses the exact same bridge a real phone call does."""

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def send(self, text: str) -> None:
        await self._ws.send_text(text)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self._ws.receive_text()
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise StopAsyncIteration from exc


def build_dashboard(
    cfg: ClinicConfig,
    pool_getter,
    password: str,
    *,
    telnyx: Any | None = None,
    from_number_getter=None,
    connect_gemini=None,
    tool_handler_getter=None,
):
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])
    guard = Depends(require_session(password))

    def _send_ready() -> str | None:
        """Return the from-number if outbound SMS can be sent, else None."""
        if telnyx is None:
            return None
        return (from_number_getter() if from_number_getter else None)

    async def _inbox_count(pool) -> int:
        if pool is None:
            return 0
        try:
            return await store.count_awaiting_reply(pool)
        except Exception:
            log.debug("inbox badge count failed", exc_info=True)
            return 0

    def _no_db(title: str, active: str) -> HTMLResponse:
        return HTMLResponse(
            shell(title, active, cfg, empty_state("alert", "No database", "No database is configured, so there is nothing to show yet."))
        )

    # --- calls ----------------------------------------------------------------

    @router.get("", response_class=HTMLResponse)
    async def calls(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return _no_db("Calls", "calls")
        rows = await pool.fetch(
            "SELECT from_number, started_at, ended_at, outcome, transcript"
            " FROM calls ORDER BY started_at DESC LIMIT 40"
        )
        if not rows:
            body = empty_state("phone", "No calls yet", "When the agent answers a call, the transcript and outcome show up here.")
        else:
            trs = [
                "<tr>"
                f'<td class="mono">{html.escape(str(r["from_number"] or "-"))}</td>'
                f'<td class="mono subtle">{local(r["started_at"], cfg)}</td>'
                f"<td>{_badge(r['outcome'])}</td>"
                f"<td>{render_transcript(r['transcript'])}</td>"
                "</tr>"
                for r in rows
            ]
            body = _table(["From", "Started", "Outcome", "Transcript"], trs)
        return HTMLResponse(shell("Calls", "calls", cfg, body,
                                  lead="What the agent said, most recent first",
                                  inbox_count=await _inbox_count(pool)))

    # --- appointments ---------------------------------------------------------

    @router.get("/appointments", response_class=HTMLResponse)
    async def appointments(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return _no_db("Appointments", "appointments")
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
        if not rows:
            body = empty_state("calendar", "Nothing booked", "Upcoming appointments will appear here as they are booked.")
        else:
            trs = [
                "<tr>"
                f'<td class="mono">{local(r["starts_at"], cfg)}</td>'
                f"<td>{html.escape(r['patient'])}</td>"
                f'<td class="mono subtle">{html.escape(r["phone"])}</td>'
                f"<td>{html.escape(r['provider'])}</td>"
                f"<td>{html.escape(r['type'])}</td>"
                f"<td>{_badge(r['status'])}</td>"
                f'<td class="subtle">{html.escape(r["source"])}</td>'
                "</tr>"
                for r in rows
            ]
            body = _table(["When", "Patient", "Phone", "Provider", "Type", "Status", "Source"], trs)
        return HTMLResponse(shell("Appointments", "appointments", cfg, body,
                                  lead="Upcoming bookings",
                                  inbox_count=await _inbox_count(pool)))

    # --- reminders ------------------------------------------------------------

    @router.get("/reminders", response_class=HTMLResponse)
    async def reminders(_=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return _no_db("Reminders", "reminders")
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
        if not rows:
            body = empty_state("bell", "No reminders yet", "Once the reminder worker runs, each send (or dry-run) is logged here.")
        else:
            trs = [
                "<tr>"
                f'<td class="mono">{local(r["starts_at"], cfg)}</td>'
                f'<td class="mono subtle">{html.escape(r["phone"])}</td>'
                f"<td>{_badge(r['status'])}</td>"
                f'<td class="num mono">{r["attempts"]}</td>'
                f'<td style="max-width:44ch">{html.escape(r["body"] or "-")}</td>'
                f'<td class="subtle">{html.escape(r["last_error"] or "")}</td>'
                "</tr>"
                for r in rows
            ]
            body = _table(["Appointment", "Phone", "Status", "Tries", "Message", "Error"], trs)
        return HTMLResponse(shell("Reminders", "reminders", cfg, body,
                                  lead="Every reminder send, newest first",
                                  inbox_count=await _inbox_count(pool)))

    # --- inbox ----------------------------------------------------------------

    def _render_thread(thread: store.Thread, *, from_number: str | None, notice: str) -> str:
        title = html.escape(thread.name) if thread.name and not thread.name.startswith("(") else html.escape(thread.phone)
        bubbles = []
        last_day = ""
        for m in thread.messages:
            day = local(m.created_at, cfg, fmt="%A, %d %B")
            if day != last_day:
                bubbles.append(f'<div style="text-align:center;font-size:11px;color:var(--ink-3);margin:6px 0">{day}</div>')
                last_day = day
            stamp = local(m.created_at, cfg, fmt="%H:%M")
            if m.direction == "inbound":
                bubbles.append(
                    f'<div class="bubble in">{html.escape(m.body)}<span class="stamp">{stamp}</span></div>'
                )
            else:
                mod = ""
                ktag = ""
                if m.status in ("failed", "blocked"):
                    mod = f" {m.status}"
                elif m.kind == "reminder":
                    mod = " reminder"
                if m.kind == "reminder":
                    ktag = '<span class="kindtag">Reminder</span>'
                elif m.kind == "auto_reply":
                    ktag = '<span class="kindtag">Auto</span>'
                status_note = ""
                if m.status == "failed":
                    status_note = " · failed"
                elif m.status == "blocked":
                    status_note = " · blocked (opted out)"
                elif m.status == "delivered":
                    status_note = " · delivered"
                bubbles.append(
                    f'<div class="bubble out{mod}">{ktag}{html.escape(m.body)}'
                    f'<span class="stamp">{stamp}{status_note}</span></div>'
                )

        stream = "".join(bubbles) or empty_state("chat-empty", "No messages", "This conversation is empty.")

        optout_bar = ""
        composer = ""
        if thread.opted_out:
            optout_bar = f'<div class="optout-bar">{icon("alert")} This number has opted out of SMS. Replies are disabled.</div>'
        elif from_number:
            composer = f"""
            <div class="composer">
              <form method="post" action="/dashboard/inbox/send">
                <input type="hidden" name="to" value="{html.escape(thread.phone)}">
                <div class="grow"><textarea name="text" rows="1" required
                     placeholder="Message {title}…" aria-label="Message"></textarea></div>
                <button class="btn btn-primary" type="submit">{icon("send")} Send</button>
              </form>
            </div>"""
        else:
            optout_bar = f'<div class="optout-bar" style="background:var(--mute-bg);color:var(--mute-ink)">{icon("alert")} Sending is not configured (set TELNYX_NUMBER). This inbox is read-only.</div>'

        notice_html = ""
        if notice == "sent":
            notice_html = '<div class="notice ok">Message sent.</div>'
        elif notice == "blocked":
            notice_html = '<div class="notice bad">Not sent — this number has opted out.</div>'
        elif notice == "failed":
            notice_html = '<div class="notice bad">Could not send. Please try again.</div>'
        elif notice == "empty":
            notice_html = '<div class="notice bad">Nothing to send — the message was empty.</div>'

        return f"""<div class="card convo">
          <div class="convo-head">
            <div class="avatar">{_initials(thread.name, thread.phone)}</div>
            <div><b>{title}</b><div class="num mono">{html.escape(thread.phone)}</div></div>
          </div>
          {optout_bar}{notice_html}
          <div class="stream" id="stream">{stream}</div>
          {composer}
        </div>
        <script>var s=document.getElementById('stream');if(s)s.scrollTop=s.scrollHeight;</script>"""

    def _render_list(threads: list[store.ThreadSummary], active_phone: str | None) -> str:
        rows = []
        for t in threads:
            name = html.escape(t.name) if t.name and not t.name.startswith("(") else html.escape(t.phone)
            on = " on" if t.phone == active_phone else ""
            rows.append(f"""<a class="thread-row{on}" href="/dashboard/inbox?to={theme_quote(t.phone)}">
              <div class="avatar">{_initials(t.name, t.phone)}</div>
              <div class="who2">
                <div class="nm"><b>{name}</b><time>{local(t.last_at, cfg, fmt="%d %b")}</time></div>
                <div class="prev">{_preview(t.last_body, t.last_direction)}</div>
              </div></a>""")
        inner = "".join(rows) or empty_state("inbox", "No conversations", "Inbound texts and replies will appear here.")
        return f'<div class="threads"><div class="card">{inner}</div></div>'

    @router.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request, to: str | None = None, sent: str | None = None, _=guard) -> HTMLResponse:
        pool = pool_getter()
        if pool is None:
            return _no_db("Inbox", "inbox")
        threads = await store.list_threads(pool, limit=100)
        active = to
        if active is None and threads:
            active = threads[0].phone

        if active:
            thread = await store.fetch_thread(pool, phone=active)
            right = _render_thread(thread, from_number=_send_ready(), notice=sent or "")
        else:
            right = f'<div class="card convo">{empty_state("chat-empty", "Pick a conversation", "Choose a patient on the left to read the thread and reply.")}</div>'

        body = f'<div class="inbox">{_render_list(threads, active)}{right}</div>'
        return HTMLResponse(shell("Inbox", "inbox", cfg, body,
                                  lead="Two-way SMS with patients",
                                  inbox_count=len([t for t in threads if t.last_direction == "inbound"]),
                                  full_bleed=True))

    @router.post("/inbox/send")
    async def inbox_send(request: Request, _=guard):
        form = await read_form(request)
        to = (form.get("to") or "").strip()
        text = form.get("text") or ""
        pool = pool_getter()
        from_number = _send_ready()
        dest = f"/dashboard/inbox?to={theme_quote(to)}"
        if pool is None or not from_number:
            return RedirectResponse(f"{dest}&sent=failed", status_code=303)
        if not text.strip():
            return RedirectResponse(f"{dest}&sent=empty", status_code=303)
        result = await outbound.send_message(
            pool, telnyx=telnyx, from_number=from_number, to=to, text=text
        )
        flag = "sent" if result.ok else ("blocked" if result.reason and "opted out" in result.reason else "failed")
        return RedirectResponse(f"{dest}&sent={flag}", status_code=303)

    # --- test the agent (browser voice) ---------------------------------------

    @router.get("/test", response_class=HTMLResponse)
    async def test_page(_=guard) -> HTMLResponse:
        return HTMLResponse(render_test_page(
            cfg, enabled=connect_gemini is not None,
            inbox_count=await _inbox_count(pool_getter()),
        ))

    @router.websocket("/testcall")
    async def test_call(websocket: WebSocket) -> None:
        # The WebSocket carries no Depends guard, so authenticate the session cookie by
        # hand -- otherwise this would be an open, Gemini-billed voice endpoint.
        if not verify_token(websocket.cookies.get(SESSION_COOKIE), password):
            await websocket.close(code=1008)
            return
        if connect_gemini is None:
            await websocket.close(code=1011)  # nothing to connect the audio to
            return
        await websocket.accept()

        async def sink(role: str, text: str) -> None:
            try:
                await websocket.send_text(json.dumps({"event": "transcript", "role": role, "text": text}))
            except Exception:
                log.debug("could not forward a transcript fragment", exc_info=True)

        async def _error(message: str) -> None:
            try:
                await websocket.send_text(json.dumps({"event": "error", "message": message}))
            except Exception:
                log.debug("could not send an error frame to the test console", exc_info=True)

        session = CallSession(
            telnyx=_BrowserSocket(websocket),
            connect_gemini=connect_gemini,
            tool_handler=tool_handler_getter() if tool_handler_getter else None,
            on_transcript=sink,
            # Tear down fast on reload/stop: unlike a phone call there is no carrier
            # holding the line, so we don't need the long drain -- and a slow teardown
            # would leave a stale Gemini session overlapping the next one on reconnect.
            drain_timeout=1.5,
        )
        try:
            outcome = await session.run()
        except Exception:
            log.exception("browser test call failed")
            await _error("The AI session ended unexpectedly. Check the server logs.")
            return
        # Don't fail silently: if the bridge never managed to open a Gemini session, tell
        # the browser instead of leaving it listening into a dead connection.
        if outcome.ended_reason == "reconnect_limit":
            await _error("Couldn't connect to the AI — the GEMINI_API_KEY was rejected or the "
                         "voice model isn't enabled for it. See the server logs.")

    return router


def theme_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
