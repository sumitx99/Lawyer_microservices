"""Login for both consoles. Same routes under /api/auth/admin and /api/auth/iam, each with
its own session cookie.

With AUTH_PROVIDER=workos people sign in on the WorkOS hosted page (routers/hosted_auth.py)
and these password routes answer HOSTED_SIGN_IN. With AUTH_PROVIDER=dev (local + tests) the
flow is: login -> [new-password] -> [mfa/setup -> mfa/setup/verify | mfa/verify] -> session,
each step returning an encrypted, 5-minute `challenge` the client posts back.

session, logout and logout-all work the same for both providers."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from pymicroservices.app.utilities.validators import Email
from sqlalchemy import select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import ApiError
from pymicroservices.app.firm_models import Admin, Firm, IamUser
from pymicroservices.app.security import crypto, sessions
from pymicroservices.app.security.guard import public, require_session, resolve
from pymicroservices.app.security.providers import AuthStep, get_provider, open_challenge, seal_challenge
from pymicroservices.app.services.firm import audit, ratelimit
from pymicroservices.app.services.firm.serialize import principal_out

STEP_STATUS = {
    "new_password_required": ("NEW_PASSWORD_REQUIRED", "new_password"),
    "email_verification_required": ("EMAIL_VERIFICATION_REQUIRED", "email"),
    "mfa_required": ("MFA_REQUIRED", "mfa"),
    "mfa_setup_required": ("MFA_SETUP_REQUIRED", "mfa_setup"),
}


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class NewPasswordIn(BaseModel):
    challenge: str
    new_password: str = Field(min_length=1, max_length=256)


class CodeIn(BaseModel):
    challenge: str
    code: str = Field(min_length=6, max_length=8)


class InviteAcceptIn(BaseModel):
    token: str = Field(min_length=1, max_length=300)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(default="", max_length=100)
    password: str = Field(min_length=1, max_length=256)


class ChallengeIn(BaseModel):
    challenge: str


class ForgotIn(BaseModel):
    email: Email


class ResetIn(BaseModel):
    email: Email
    code: str = Field(min_length=4, max_length=12)
    new_password: str = Field(min_length=1, max_length=256)


def _principal(db: Session, console: str, email: str):
    model = Admin if console == "admin" else IamUser
    return db.scalar(select(model).where(model.email == email.lower()))


def _org(db: Session, person) -> str | None:
    firm = db.get(Firm, person.firm_id) if person else None
    return firm.workos_organization_id if firm else None


def _require_mfa(db: Session, console: str, person) -> bool:
    if console == "admin":
        return get_settings().admin_require_mfa  # always true in production
    firm = db.get(Firm, person.firm_id) if person else None
    return bool(firm and firm.require_mfa)


def build_router(console: str) -> APIRouter:
    router = APIRouter(prefix=f"/api/auth/{console}", tags=[f"auth-{console}"])
    open_route = Depends(public("login flow, rate-limited"))

    def finish(db: Session, request: Request, response: Response, email: str, step: AuthStep):
        person = _principal(db, console, email)
        if step.status == "sso_required":
            raise ApiError(400, "SSO_REQUIRED", "Your firm signs in with single sign-on. Contact your administrator.")
        if step.status in ("invalid", "disabled") or not person or person.status == "deactivated":
            audit.record(db, "auth.login_failed", firm_id=person.firm_id if person else None, actor_type="anonymous",
                         ip=sessions.client_ip(request), data={"console": console, "email": email})
            db.commit()
            raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password.")
        if step.status in STEP_STATUS:
            label, stage = STEP_STATUS[step.status]
            return {"status": label, "challenge": seal_challenge(console, email, stage, step.state)}
        # ok: bind the provider identity to our row, then open a session
        if person.provider_subject and step.subject and person.provider_subject != step.subject:
            raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password.")
        person.provider_subject = person.provider_subject or step.subject
        if person.status == "invited":
            person.status = "active"
        person.last_login_at = datetime.now(timezone.utc)
        sessions.create_session(db, request, response, console, person.id, person.firm_id, step.refresh_token,
                                step.session_id)
        audit.record(db, "auth.login", firm_id=person.firm_id, actor_type=console, actor_id=person.id,
                     ip=sessions.client_ip(request))
        db.commit()
        return {"status": "success", "principal": principal_out(db, console, person)}

    @router.post("/login", dependencies=[open_route])
    async def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
        email = body.email.lower()
        ratelimit.hit(db, request, f"login-{console}", 5, 60)
        ratelimit.hit(db, request, f"login-{console}-email", 10, 900, email)
        person = _principal(db, console, email)
        step = await get_provider(console).authenticate(db, email, body.password, _require_mfa(db, console, person),
                                                        organization_id=_org(db, person))
        return finish(db, request, response, email, step)

    @router.post("/email/verify", dependencies=[open_route])
    async def email_verify(body: CodeIn, request: Request, response: Response, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"email-{console}", 5, 300)
        email, state = open_challenge(body.challenge, console, "email")
        provider = get_provider(console)
        if not hasattr(provider, "verify_email"):
            raise ApiError(400, "NOT_SUPPORTED", "Email verification isn't used here.")
        return finish(db, request, response, email, await provider.verify_email(db, email, state, body.code))

    @router.post("/invite/accept", dependencies=[open_route])
    async def accept_invite(body: InviteAcceptIn, request: Request, response: Response, db: Session = Depends(get_db)):
        """Our /accept-invite page: set name + password for an invited email, then sign in."""
        ratelimit.hit(db, request, f"invite-{console}", 5, 900)
        provider = get_provider(console)
        if not hasattr(provider, "accept_invitation"):
            raise ApiError(400, "NOT_SUPPORTED", "Use the temporary password from your invite.")
        invitation = await provider.invitation_by_token(body.token)
        if not invitation:
            raise ApiError(400, "INVITE_INVALID", "This invitation is invalid, used or expired. Ask for a new one.")
        email = invitation["email"].lower()
        person = _principal(db, console, email)
        if not person or person.status == "deactivated":
            raise ApiError(400, "INVITE_INVALID", "This invitation isn't for this console.")
        person.name = " ".join(p for p in (body.first_name.strip(), body.last_name.strip()) if p) or person.name
        step = await provider.accept_invitation(db, invitation, body.first_name.strip(), body.last_name.strip(),
                                                body.password, _require_mfa(db, console, person), _org(db, person))
        return finish(db, request, response, email, step)

    @router.post("/new-password", dependencies=[open_route])
    async def new_password(body: NewPasswordIn, request: Request, response: Response, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"newpw-{console}", 5, 900)
        email, state = open_challenge(body.challenge, console, "new_password")
        person = _principal(db, console, email)
        step = await get_provider(console).complete_new_password(db, email, state, body.new_password,
                                                    _require_mfa(db, console, person))
        return finish(db, request, response, email, step)

    @router.post("/mfa/verify", dependencies=[open_route])
    async def mfa_verify(body: CodeIn, request: Request, response: Response, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"mfa-{console}", 5, 300)
        email, state = open_challenge(body.challenge, console, "mfa")
        return finish(db, request, response, email, await get_provider(console).verify_mfa(db, email, state, body.code))

    @router.post("/mfa/setup", dependencies=[open_route])
    async def mfa_setup(body: ChallengeIn, request: Request, db: Session = Depends(get_db)):
        email, state = open_challenge(body.challenge, console, "mfa_setup")
        step = await get_provider(console).begin_mfa_setup(db, email, state)
        return {"status": "MFA_SETUP", "secret": step.mfa_secret, "otpauth_uri": crypto.totp_uri(step.mfa_secret, email),
                "challenge": seal_challenge(console, email, "mfa_setup_verify", step.state)}

    @router.post("/mfa/setup/verify", dependencies=[open_route])
    async def mfa_setup_verify(body: CodeIn, request: Request, response: Response, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"mfa-{console}", 5, 300)
        email, state = open_challenge(body.challenge, console, "mfa_setup_verify")
        return finish(db, request, response, email, await get_provider(console).finish_mfa_setup(db, email, state, body.code))

    @router.post("/forgot", dependencies=[open_route])
    async def forgot(body: ForgotIn, request: Request, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"forgot-{console}", 3, 600)
        email = body.email.lower()
        person = _principal(db, console, email)
        code = None
        if person and person.status != "deactivated":
            code = await get_provider(console).forgot_password(db, email)
        out = {"status": "success", "message": "If that email has an account, a reset code is on its way."}
        if get_settings().dev_auth and code:
            out["dev_code"] = code  # dev provider only, never in production
        return out

    @router.post("/reset", dependencies=[open_route])
    async def reset(body: ResetIn, request: Request, db: Session = Depends(get_db)):
        ratelimit.hit(db, request, f"reset-{console}", 5, 900)
        email = body.email.lower()
        await get_provider(console).reset_password(db, email, body.code, body.new_password)
        person = _principal(db, console, email)
        if person:
            sessions.revoke_all(db, console, person.id)  # a reset ends every existing session
            audit.record(db, "auth.password_reset", firm_id=person.firm_id, actor_type=console, actor_id=person.id,
                         ip=sessions.client_ip(request))
        db.commit()
        return {"status": "success", "message": "Password updated. Sign in with your new password."}

    @router.get("/session", dependencies=[Depends(public("returns 401 when signed out"))])
    async def session(request: Request, db: Session = Depends(get_db)):
        ctx = await resolve(request, db, console)
        model = Admin if console == "admin" else IamUser
        return {"status": "Authorized", "principal": principal_out(db, console, db.get(model, ctx.who.id))}

    @router.post("/logout", dependencies=[Depends(public("ends the caller's own session"))])
    async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
        row = sessions.load_session(db, request, console)
        logout_url = None
        if row:
            if get_provider(console).hosted and row.provider_session_id:
                # Also end the WorkOS session, or the hosted page would sign them straight back in.
                login = "/admin/login" if console == "admin" else "/login"
                logout_url = get_provider(console).logout_url(row.provider_session_id, f"{get_settings().app_url}{login}")
            sessions.revoke(db, row)
            audit.record(db, "auth.logout", firm_id=row.firm_id, actor_type=console, actor_id=row.principal_id,
                         ip=sessions.client_ip(request))
            db.commit()
        sessions.clear_cookies(response, console)
        return {"status": "success", "logout_url": logout_url}

    @router.post("/logout-all")
    async def logout_all(request: Request, response: Response, db: Session = Depends(get_db),
                         ctx=Depends(require_session())):
        if ctx.who.console != console:
            raise ApiError(400, "WRONG_CONSOLE", "Sign out from the console you are signed in to.")
        logout_url = None
        if get_provider(console).hosted and ctx.session.provider_session_id:
            login = "/admin/login" if console == "admin" else "/login"
            logout_url = get_provider(console).logout_url(ctx.session.provider_session_id, f"{get_settings().app_url}{login}")
        count = sessions.revoke_all(db, console, ctx.who.id)
        await get_provider(console).global_sign_out(db, ctx.who.email)
        audit.record_ctx(db, ctx, "auth.logout_all", sessions=count)
        db.commit()
        sessions.clear_cookies(response, console)
        return {"status": "success", "sessions_ended": count, "logout_url": logout_url}

    return router
