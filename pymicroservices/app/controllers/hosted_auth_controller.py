"""Shared WorkOS auth routes (not tied to one console).

  GET  /api/auth/config                 which sign-in the frontend should show
  GET  /api/auth/invitations/{token}    our /accept-invite page: who is invited, to which console
  POST /api/auth/reset-password         our /reset-password page: token from the WorkOS email
  GET  /api/auth/{console}/start        hosted AuthKit redirect (kept for per-firm SSO later)
  GET  /api/auth/callback               hosted AuthKit return

WorkOS proves who the person is (password, MFA, SSO). Our tables decide whether that
person may enter the Admin console (`admins`) or the workspace (`iam_users`), and we open
our own session cookie. Nobody gets in just by having a WorkOS account."""
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import ApiError, bad_request
from pymicroservices.app.firm_models import Admin, IamUser
from pymicroservices.app.security import crypto, sessions
from pymicroservices.app.security.guard import public
from pymicroservices.app.security.providers import WorkOSProvider, get_provider
from pymicroservices.app.services.firm import audit, ratelimit

router = APIRouter(prefix="/api/auth", tags=["auth-hosted"])
STATE_COOKIE = "lx_oauth"
HOMES = {"iam": "/app", "admin": "/admin"}
LOGINS = {"iam": "/login", "admin": "/admin/login"}


def _hosted() -> WorkOSProvider:
    provider = get_provider("iam")
    if not isinstance(provider, WorkOSProvider):
        raise bad_request("NOT_HOSTED", "Hosted sign-in isn't enabled.")
    return provider


def _redirect_uri() -> str:
    return f"{get_settings().app_url}/api/auth/callback"


def _fail(console: str | None, reason: str) -> RedirectResponse:
    response = RedirectResponse(f"{get_settings().app_url}{LOGINS[console or 'iam']}?{urlencode({'error': reason})}",
                                status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    return response


def _find(db: Session, model, subject: str, email: str):
    return db.scalar(select(model).where(model.provider_subject == subject)) or \
        db.scalar(select(model).where(model.email == email))


@router.get("/config", dependencies=[Depends(public("tells the login page which flow to show"))])
def auth_config():
    return {"mode": "hosted" if get_provider("iam").hosted else "password", "provider": get_settings().auth_provider}


@router.get("/invitations/{token}", dependencies=[Depends(public("invite link token is the credential"))])
async def invitation_info(token: str, request: Request, db: Session = Depends(get_db)):
    ratelimit.hit(db, request, "invite-info", 20, 60)
    invitation = await _hosted().invitation_by_token(token)
    if not invitation:
        raise ApiError(404, "INVITE_INVALID", "This invitation is invalid, used or expired. Ask for a new one.")
    email = invitation["email"].lower()
    user = db.scalar(select(IamUser).where(IamUser.email == email))
    admin = db.scalar(select(Admin).where(Admin.email == email))
    person, console = (user, "iam") if user else (admin, "admin")
    if not person or person.status == "deactivated":
        raise ApiError(404, "INVITE_INVALID", "This invitation is no longer valid. Ask your administrator.")
    first, _, last = person.name.partition(" ")
    return {"email": email, "console": console, "first_name": first, "last_name": last,
            "has_account": bool(await _hosted().user_by_email(email))}


class ResetIn(BaseModel):
    token: str = Field(min_length=8, max_length=300)
    new_password: str = Field(min_length=1, max_length=256)


@router.post("/reset-password", dependencies=[Depends(public("reset link token is the credential"))])
async def reset_password(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    ratelimit.hit(db, request, "reset-token", 5, 900)
    user = await _hosted().reset_password_with_token(body.token, body.new_password)
    email = (user.get("email") or "").lower()
    for model, console in ((IamUser, "iam"), (Admin, "admin")):
        person = db.scalar(select(model).where(model.email == email)) if email else None
        if person:
            sessions.revoke_all(db, console, person.id)  # WorkOS revokes its sessions too
            audit.record(db, "auth.password_reset", firm_id=person.firm_id, actor_type=console, actor_id=person.id,
                         ip=sessions.client_ip(request))
    db.commit()
    return {"status": "success", "message": "Password updated. Sign in with your new password."}


@router.get("/{console}/start", dependencies=[Depends(public("redirects to the hosted sign-in page"))])
def start(console: Literal["iam", "admin"], request: Request, next: str = "", login_hint: str = "",
          db: Session = Depends(get_db)):
    provider = _hosted()
    ratelimit.hit(db, request, f"start-{console}", 20, 60)
    nonce = crypto.random_token(16)
    target = next if next.startswith(HOMES[console]) else HOMES[console]
    state = crypto.sign({"c": console, "n": target, "x": nonce}, "oauth", 600)
    response = RedirectResponse(provider.authorize_url(_redirect_uri(), state, login_hint or None), status_code=303)
    s = get_settings()
    response.set_cookie(STATE_COOKIE, nonce, max_age=600, httponly=True, secure=s.cookie_secure, samesite="lax",
                        path="/api/auth", domain=s.cookie_domain)
    return response


@router.get("/callback", dependencies=[Depends(public("WorkOS redirect with a one-time code"))])
async def callback(request: Request, code: str = "", state: str = "", error: str = "",
                   db: Session = Depends(get_db)):
    provider = _hosted()
    ratelimit.hit(db, request, "callback", 20, 60)
    console, target = None, None
    if state:
        body = crypto.unsign(state, "oauth")
        if not body or body.get("x") != request.cookies.get(STATE_COOKIE):
            return _fail(None, "expired")
        console, target = body["c"], body["n"]
    # No state: WorkOS-initiated sign-in, e.g. right after accepting an invitation email.
    if error or not code:
        return _fail(console, "cancelled")
    try:
        identity = await provider.exchange_code(code, sessions.client_ip(request), request.headers.get("user-agent"))
    except ApiError:
        return _fail(console, "provider")
    if not identity.subject or not identity.email:
        return _fail(console, "provider")
    if not identity.email_verified:
        return _fail(console, "email_not_verified")

    if console:
        person = _find(db, Admin if console == "admin" else IamUser, identity.subject, identity.email)
    else:
        person = _find(db, IamUser, identity.subject, identity.email)
        console = "iam"
        if not person:
            person, console = _find(db, Admin, identity.subject, identity.email), "admin"
    if not person or person.status == "deactivated" \
            or (person.provider_subject and person.provider_subject != identity.subject):
        audit.record(db, "auth.login_failed", firm_id=person.firm_id if person else None, actor_type="anonymous",
                     ip=sessions.client_ip(request), data={"console": console, "email": identity.email, "via": "workos"})
        db.commit()
        return _fail(console, "no_account")

    person.provider_subject = identity.subject
    if person.status == "invited":
        person.status = "active"
    person.last_login_at = datetime.now(timezone.utc)
    home = HOMES[console]
    response = RedirectResponse(f"{get_settings().app_url}{target if target and target.startswith(home) else home}",
                                status_code=303)
    sessions.create_session(db, request, response, console, person.id, person.firm_id, identity.refresh_token,
                            identity.session_id)
    audit.record(db, "auth.login", firm_id=person.firm_id, actor_type=console, actor_id=person.id,
                 ip=sessions.client_ip(request), data={"via": "workos"})
    db.commit()
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    return response
