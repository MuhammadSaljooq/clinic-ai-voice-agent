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

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from clinic_agent.config import ClinicConfig
from clinic_agent.web import theme

log = logging.getLogger(__name__)

SESSION_COOKIE = "clinic_session"
SESSION_MAX_AGE = 12 * 60 * 60  # 12 hours: a working day, then sign in again.
LOGIN_PATH = "/login"
DEFAULT_LANDING = "/dashboard/inbox"


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
) -> str:
    import html

    brand = "Next Higher Solutions"
    err = f'<div class="login-err" role="alert">{html.escape(error)}</div>' if error else ""
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


def build_auth_router(cfg: ClinicConfig, password: str, *, username: str | None = None) -> APIRouter:
    """`username` optional: when set, the login page shows a username field and requires
    it to match (in addition to the password). When None, it's password-only."""
    router = APIRouter(tags=["auth"])
    require_username = bool(username)

    @router.get(LOGIN_PATH, response_class=HTMLResponse)
    async def login_form(request: Request, next: str = DEFAULT_LANDING) -> Response:
        if verify_token(request.cookies.get(SESSION_COOKIE), password):
            return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        return HTMLResponse(render_login(cfg, next_url=_safe_next(next), require_username=require_username))

    @router.post(LOGIN_PATH, response_class=HTMLResponse)
    async def login_submit(request: Request) -> Response:
        form = await read_form(request)
        supplied = str(form.get("password") or "")
        supplied_user = str(form.get("username") or "")
        target = _safe_next(str(form.get("next") or DEFAULT_LANDING))

        ok = hmac.compare_digest(supplied, password)
        if require_username:
            ok = hmac.compare_digest(supplied_user, username) and ok
        if not ok:
            log.warning("failed dashboard login attempt")
            message = ("That username or password is not correct." if require_username
                       else "That password is not correct.")
            return HTMLResponse(
                render_login(cfg, error=message, next_url=target, require_username=require_username),
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
