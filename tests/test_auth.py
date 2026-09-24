"""Session login: token signing, expiry, and the login/guard/logout flow."""

from __future__ import annotations

import pathlib

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from clinic_agent.config import load_config
from clinic_agent.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Workspace,
    build_auth_router,
    issue_token,
    require_session,
    verify_token,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config.yaml")
PW = "correct horse battery staple"


# --- token ---------------------------------------------------------------------


def test_a_freshly_issued_token_verifies():
    assert verify_token(issue_token(PW, now=1000.0), PW, now=1000.0)


def test_a_token_signed_with_a_different_secret_fails():
    assert not verify_token(issue_token(PW, now=1000.0), "other", now=1000.0)


def test_a_tampered_token_fails():
    token = issue_token(PW, now=1000.0)
    issued, _, sig = token.partition(".")
    forged = f"{int(issued) - 5000}.{sig}"  # keep signature, move the timestamp back
    assert not verify_token(forged, PW, now=1000.0)


def test_an_expired_token_fails():
    token = issue_token(PW, now=1000.0)
    assert not verify_token(token, PW, now=1000.0 + SESSION_MAX_AGE + 1)


def test_a_token_from_the_future_fails():
    token = issue_token(PW, now=5000.0)
    assert not verify_token(token, PW, now=1000.0)


@pytest.mark.parametrize("bad", [None, "", "no-dot", "123.", ".sig", "abc.def"])
def test_malformed_tokens_fail(bad):
    assert not verify_token(bad, PW, now=1000.0)


# --- flow ----------------------------------------------------------------------


_GUARD = Depends(require_session(PW))


def _app():
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PW))

    @app.get("/guarded")
    async def guarded(_=_GUARD):
        return {"ok": True}

    return app


async def test_login_sets_a_session_cookie_and_it_opens_the_guard():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        assert (await c.get("/guarded")).status_code == 303  # no session -> redirect
        r = await c.post(
            "/login", content=f"password={PW}",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 303
        assert SESSION_COOKIE in r.headers.get("set-cookie", "")
        assert (await c.get("/guarded")).json() == {"ok": True}


async def test_login_cookie_is_httponly_and_lax():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post(
            "/login", content=f"password={PW}",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        cookie = r.headers.get("set-cookie", "").lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie


async def test_logout_clears_the_session():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.post("/login", content=f"password={PW}",
                     headers={"content-type": "application/x-www-form-urlencoded"})
        assert (await c.get("/guarded")).status_code == 200
        await c.post("/logout")
        assert (await c.get("/guarded")).status_code == 303


async def test_username_and_password_both_required_when_username_configured():
    FORM = {"content-type": "application/x-www-form-urlencoded"}
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PW, username="adrian@nhs.com"))
    guard = require_session(PW)

    @app.get("/guarded")
    async def guarded(_=Depends(guard)):
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # the page now shows a username field
        page = (await c.get("/login")).text
        assert 'id="user"' in page and 'name="username"' in page
        # right password but wrong username -> rejected
        bad = await c.post("/login", content=f"username=nope&password={PW}", headers=FORM)
        assert bad.status_code == 401
        # both correct -> session opens
        ok = await c.post("/login", content=f"username=adrian@nhs.com&password={PW}", headers=FORM)
        assert ok.status_code == 303
        assert (await c.get("/guarded")).json() == {"ok": True}


async def test_any_of_several_configured_usernames_can_sign_in():
    """A comma-separated DASHBOARD_USERNAME accepts each username, all sharing the password."""
    FORM = {"content-type": "application/x-www-form-urlencoded"}
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PW, username="adrian@nhs.com, tasktitan@nhs.com"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for user in ("adrian@nhs.com", "tasktitan@nhs.com"):
            r = await c.post("/login", content=f"username={user}&password={PW}", headers=FORM)
            assert r.status_code == 303, f"{user} should be accepted"
        # an unlisted username is still rejected
        bad = await c.post("/login", content=f"username=intruder@nhs.com&password={PW}", headers=FORM)
        assert bad.status_code == 401


async def test_an_open_redirect_next_is_refused():
    """?next must stay same-site; an absolute URL falls back to the default landing."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post(
            "/login", content=f"password={PW}&next=https://evil.example/steal",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.headers["location"] == "/dashboard/inbox"


# --- workspace picker ----------------------------------------------------------

FORM = {"content-type": "application/x-www-form-urlencoded"}

CLINIC_WS = Workspace("clinic", "Northside Clinic", "/dashboard/inbox", "/dashboard", "inbox")
TRAILER_WS = Workspace("trailer", "Ridgeline Trailers", "/dashboard/trailer/test", "/dashboard/trailer", "calendar")
HANDYMAN_WS = Workspace("handyman", "Task Titan", "/dashboard/handyman/voice", "/dashboard/handyman", "mic")


def _ws_app(workspaces=(CLINIC_WS, TRAILER_WS)):
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PW, workspaces=list(workspaces)))
    return app


async def test_the_picker_is_hidden_when_only_one_workspace_exists():
    app = FastAPI()
    app.include_router(build_auth_router(CFG, PW, workspaces=[CLINIC_WS]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        page = (await c.get("/login")).text
        assert 'name="workspace"' not in page


async def test_the_picker_offers_every_configured_workspace():
    async with AsyncClient(transport=ASGITransport(app=_ws_app((CLINIC_WS, TRAILER_WS, HANDYMAN_WS))), base_url="http://t") as c:
        page = (await c.get("/login")).text
        assert 'value="clinic"' in page and 'value="trailer"' in page and 'value="handyman"' in page
        assert "Task Titan" in page


async def test_choosing_the_clinic_workspace_lands_on_the_clinic_console():
    async with AsyncClient(transport=ASGITransport(app=_ws_app()), base_url="http://t") as c:
        r = await c.post("/login", content=f"password={PW}&workspace=clinic", headers=FORM)
        assert r.headers["location"] == "/dashboard/inbox"


async def test_choosing_the_handyman_workspace_lands_on_the_handyman_console():
    app = _ws_app((CLINIC_WS, TRAILER_WS, HANDYMAN_WS))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/login", content=f"password={PW}&workspace=handyman", headers=FORM)
        assert r.headers["location"] == "/dashboard/handyman/voice"


async def test_a_workspace_choice_is_ignored_when_that_agent_is_not_configured():
    """A forged workspace key falls back to the first (clinic) landing."""
    async with AsyncClient(transport=ASGITransport(app=_ws_app()), base_url="http://t") as c:
        r = await c.post("/login", content=f"password={PW}&workspace=handyman", headers=FORM)
        assert r.headers["location"] == "/dashboard/inbox"


async def test_a_next_into_another_workspace_falls_back_to_the_chosen_home():
    """Picked clinic but ?next points into handyman -> land on the clinic home."""
    app = _ws_app((CLINIC_WS, TRAILER_WS, HANDYMAN_WS))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/login",
            content=f"password={PW}&workspace=clinic&next=%2Fdashboard%2Fhandyman%2Fleads",
            headers=FORM,
        )
        assert r.headers["location"] == "/dashboard/inbox"


async def test_a_next_within_the_chosen_workspace_is_honoured():
    app = _ws_app((CLINIC_WS, TRAILER_WS, HANDYMAN_WS))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/login",
            content=f"password={PW}&workspace=handyman&next=%2Fdashboard%2Fhandyman%2Fappointments",
            headers=FORM,
        )
        assert r.headers["location"] == "/dashboard/handyman/appointments"
