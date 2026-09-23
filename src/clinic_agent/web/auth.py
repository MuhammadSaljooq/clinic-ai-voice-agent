"""Session login for the operator console.

Replaces the browser's HTTP Basic pop-up with a real login page and a signed-cookie
session -- so there is a page to brand, a sign-out that works, and no password sent on
every single request.

The cookie is stateless: `issued_at.signature`, signed with HMAC-SHA-256 keyed on the
dashboard password. No server-side session store to keep, and rotating the password
invalidates every existing session for free. The signature is verified in constant
time; a tampered or expired cookie simply fails and the guard bounces to /login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import NamedTuple

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from clinic_agent.config import ClinicConfig
from clinic_agent.web import theme

log = logging.getLogger(__name__)

SESSION_COOKIE = "clinic_session"
SESSION_MAX_AGE = 12 * 60 * 60  # 12 hours: a working day, then sign in again.
LOGIN_PATH = "/login"
DEFAULT_LANDING = "/dashboard/inbox"


class Workspace(NamedTuple):
    """One selectable console on the login picker. `prefix` scopes which pages belong to
    it (used to route a ?next to the right workspace); `icon` is a theme icon name."""

    key: str
    label: str
    landing: str
    prefix: str
    icon: str = "inbox"


def _workspace_of(path: str, workspaces: list[Workspace]) -> Workspace | None:
    """The workspace that owns a path, by longest matching prefix. The clinic prefix
    ('/dashboard') matches everything, so more specific consoles ('/dashboard/handyman')
    win for their own pages and the clinic is the natural fallback."""
    best: Workspace | None = None
    for w in workspaces:
        matches = path == w.prefix or path.startswith(w.prefix.rstrip("/") + "/")
        if matches and (best is None or len(w.prefix) > len(best.prefix)):
            best = w
    return best


def _landing_for(workspace_key: str, next_url: str | None, workspaces: list[Workspace]) -> str:
    """Where a successful login lands. The workspace the operator picked is authoritative;
    a ?next is honoured only when it belongs to that same workspace (so a session that
    expired mid-page returns there), otherwise we fall back to the workspace home."""
    chosen = next((w for w in workspaces if w.key == workspace_key), workspaces[0])
    safe = _safe_next(next_url)
    owner = _workspace_of(safe, workspaces)
    if owner is not None and owner.key == chosen.key:
        return safe
    return chosen.landing


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token(secret: str, *, now: float | None = None) -> str:
    issued = str(int(now if now is not None else time.time()))
    return f"{issued}.{_sign(issued, secret)}"


def verify_token(token: str | None, secret: str, *, now: float | None = None, max_age: int = SESSION_MAX_AGE) -> bool:
    if not token or "." not in token:
        return False
    issued_str, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(issued_str, secret)):
        return False
    try:
        issued = int(issued_str)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    # Reject the future (clock skew / tampering) and anything past its lifetime.
    return -60 <= (current - issued) <= max_age


def _is_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _safe_next(raw: str | None) -> str:
    """Only allow same-site relative redirects, never an attacker-supplied absolute URL."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return DEFAULT_LANDING


def require_session(password: str):
    """Dependency: allow the request through, or redirect to /login preserving the target."""

    async def guard(request: Request) -> None:
        if verify_token(request.cookies.get(SESSION_COOKIE), password):
            return
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"{LOGIN_PATH}?next={_quote(target)}"},
        )

    return guard


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


async def read_form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body without pulling in
    python-multipart. The console only ever posts simple flat forms, so this is all
    that is needed -- and it keeps the dependency list unchanged."""
    from urllib.parse import parse_qs

    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def render_login(
    cfg: ClinicConfig,
    *,
    error: str | None = None,
    next_url: str = DEFAULT_LANDING,
    require_username: bool = False,
    workspaces: list[Workspace] | None = None,
    selected_workspace: str | None = None,
) -> str:
    import html

    brand = "Next Higher Solutions"
    err = f'<div class="login-err" role="alert">{html.escape(error)}</div>' if error else ""

    # Workspace picker: which console the operator lands on after signing in. Only shown
    # when more than one agent is configured; otherwise it's a single-console login.
    workspace_field = ""
    if workspaces and len(workspaces) > 1:
        selected = selected_workspace or workspaces[0].key
        radios = []
        for w in workspaces:
            checked = "checked" if w.key == selected else ""
            radios.append(
                f'<input type="radio" id="ws-{html.escape(w.key)}" name="workspace" '
                f'value="{html.escape(w.key)}" {checked}>'
                f'<label for="ws-{html.escape(w.key)}">{theme.icon(w.icon)}'
                f'<span>{html.escape(w.label)}</span></label>'
            )
        # Two side-by-side reads as a toggle; three or more stack as a clear vertical list.
        cols = len(workspaces) if len(workspaces) == 2 else 1
        workspace_field = (
            f'<div class="wsseg" role="radiogroup" aria-label="Choose a workspace" '
            f'style="grid-template-columns:repeat({cols},1fr)">{"".join(radios)}</div>'
        )
    if require_username:
        username_field = """
        <div class="field" style="margin-bottom:16px">
          <label class="lbl" for="user">Username</label>
          <input id="user" name="username" type="text" autocomplete="username"
                 autofocus required placeholder="you@example.com">
        </div>"""
        pw_autofocus = ""
    else:
        # Hidden but present so password managers and assistive tech have a username.
        username_field = ('<input type="text" name="username" value="clinic" '
                          'autocomplete="username" aria-hidden="true" tabindex="-1" hidden>')
        pw_autofocus = "autofocus"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · {brand}</title>
<style>{theme.CSS}</style></head>
<body>
<div class="login-wrap">
  <div class="login-card">
    <div class="login-brand">
      <h1>{brand}</h1>
      <p>Operator console — sign in to continue</p>
    </div>
    <div class="card" style="padding:26px 24px">
      {err}
      <form method="post" action="{LOGIN_PATH}">
        <input type="hidden" name="next" value="{html.escape(next_url)}">
        {workspace_field}
        {username_field}
        <div class="field" style="margin-bottom:16px">
          <label class="lbl" for="pw">Password</label>
          <input id="pw" name="password" type="password" autocomplete="current-password"
                 {pw_autofocus} required placeholder="Enter your password">
        </div>
        <button class="btn btn-primary" type="submit">{theme.icon("logout")} Sign in</button>
      </form>
    </div>
    <div class="login-foot">Access is limited to clinic staff. Activity may be logged.</div>
  </div>
</div>
</body></html>"""


def build_auth_router(
    cfg: ClinicConfig,
    password: str,
    *,
    username: str | None = None,
    workspaces: list[Workspace] | None = None,
) -> APIRouter:
    """`username` optional: when set, the login page shows a username field and requires
    it to match (in addition to the password). When None, it's password-only.

    `workspaces` (2+) adds a picker to the login page; the choice decides which console the
    operator lands on. One session serves them all. None/one workspace = no picker."""
    router = APIRouter(tags=["auth"])
    require_username = bool(username)
    picker = workspaces if (workspaces and len(workspaces) > 1) else None

    def _selected(next_url: str) -> str | None:
        if not picker:
            return None
        owner = _workspace_of(next_url, picker)
        return owner.key if owner else picker[0].key

    @router.get(LOGIN_PATH, response_class=HTMLResponse)
    async def login_form(request: Request, next: str = DEFAULT_LANDING) -> Response:
        if verify_token(request.cookies.get(SESSION_COOKIE), password):
            return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        safe = _safe_next(next)
        return HTMLResponse(render_login(
            cfg, next_url=safe, require_username=require_username,
            workspaces=picker, selected_workspace=_selected(safe),
        ))

    @router.post(LOGIN_PATH, response_class=HTMLResponse)
    async def login_submit(request: Request) -> Response:
        form = await read_form(request)
        supplied = str(form.get("password") or "")
        supplied_user = str(form.get("username") or "")
        workspace = str(form.get("workspace") or "")
        if picker:
            target = _landing_for(workspace, form.get("next"), picker)
        else:
            target = _safe_next(str(form.get("next") or DEFAULT_LANDING))

        ok = hmac.compare_digest(supplied, password)
        if require_username:
            ok = hmac.compare_digest(supplied_user, username) and ok
        if not ok:
            log.warning("failed dashboard login attempt")
            message = ("That username or password is not correct." if require_username
                       else "That password is not correct.")
            return HTMLResponse(
                render_login(
                    cfg, error=message, next_url=target, require_username=require_username,
                    workspaces=picker, selected_workspace=(workspace or None),
                ),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            issue_token(password),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_is_secure(request),
            path="/",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        response = RedirectResponse(LOGIN_PATH, status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router
