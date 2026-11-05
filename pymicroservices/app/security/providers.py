"""Identity providers. The rest of the app talks only to `IdentityProvider`, so switching
provider touches this file only.

  WorkOSProvider  production and staging: our own sign-in screens, WorkOS called from the
                  backend (one WorkOS environment). Which console someone may enter is
                  decided by our tables (admins vs iam_users); each console keeps its own cookie.
  DevProvider     local work and tests: password + TOTP flows backed by dev_identities."""
import asyncio
import base64
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.utilities.errors import ApiError, bad_request
from pymicroservices.app.security import crypto

log = logging.getLogger("lexaior.auth")
CHALLENGE_TTL = 300


@dataclass
class AuthStep:
    # ok | new_password_required | email_verification_required | mfa_required | mfa_setup_required
    # | sso_required | invalid | disabled
    status: str
    subject: str | None = None
    refresh_token: str | None = None
    state: dict | None = None  # provider state carried inside the encrypted challenge
    mfa_secret: str | None = None
    session_id: str | None = None  # provider session id (WorkOS `sid`), for logout


def seal_challenge(console: str, email: str, stage: str, state: dict | None) -> str:
    inner = crypto.encrypt(json.dumps({"c": console, "e": email, "s": stage, "p": state or {}}))
    return crypto.sign({"d": inner}, "challenge", CHALLENGE_TTL)


def open_challenge(token: str, console: str, stage: str) -> tuple[str, dict]:
    body = crypto.unsign(token or "", "challenge")
    if not body:
        raise bad_request("CHALLENGE_EXPIRED", "This step expired. Please sign in again.")
    data = json.loads(crypto.decrypt(body["d"]))
    if data["c"] != console or data["s"] != stage:
        raise bad_request("CHALLENGE_INVALID", "Please sign in again.")
    return data["e"], data["p"]


class IdentityProvider:
    console: str
    hosted = False  # True: sign-in happens on the provider's page (redirect + callback)

    async def authenticate(self, db: Session, email: str, password: str, require_mfa: bool) -> AuthStep: ...
    async def complete_new_password(self, db: Session, email: str, state: dict, new_password: str, require_mfa: bool) -> AuthStep: ...
    async def verify_mfa(self, db: Session, email: str, state: dict, code: str) -> AuthStep: ...
    async def begin_mfa_setup(self, db: Session, email: str, state: dict) -> AuthStep: ...
    async def finish_mfa_setup(self, db: Session, email: str, state: dict, code: str) -> AuthStep: ...
    async def forgot_password(self, db: Session, email: str) -> str | None: ...
    async def reset_password(self, db: Session, email: str, code: str, new_password: str) -> None: ...
    async def reverify(self, db: Session, email: str, refresh_token: str | None) -> tuple[bool, str | None]: ...
    async def create_user(self, db: Session, email: str, name: str) -> tuple[str, str | None]: ...
    async def set_enabled(self, db: Session, email: str, enabled: bool) -> None: ...
    async def delete_user(self, db: Session, email: str, subject: str | None = None) -> None: ...
    async def global_sign_out(self, db: Session, email: str) -> None: ...


PASSWORD_RULES = "at least 12 characters, with upper and lower case letters and a number"


def check_password_policy(password: str) -> None:
    ok = len(password) >= 12 and any(c.islower() for c in password) and any(c.isupper() for c in password) \
        and any(c.isdigit() for c in password)
    if not ok:
        raise bad_request("WEAK_PASSWORD", f"Password must be {PASSWORD_RULES}.")


# --------------------------------------------------------------------------------------
# Dev provider: accounts in Postgres. Local development and tests only.
# --------------------------------------------------------------------------------------
class DevProvider(IdentityProvider):
    def __init__(self, console: str):
        self.console = console

    def _get(self, db: Session, email: str):
        from pymicroservices.app.firm_models import DevIdentity
        return db.scalar(select(DevIdentity).where(DevIdentity.console == self.console, DevIdentity.email == email))

    def _after_password(self, ident, require_mfa: bool) -> AuthStep:
        if ident.mfa_enabled:
            return AuthStep("mfa_required", subject=ident.id)
        if require_mfa:
            return AuthStep("mfa_setup_required", subject=ident.id)
        return AuthStep("ok", subject=ident.id, refresh_token=crypto.random_token())

    async def authenticate(self, db, email, password, require_mfa, organization_id=None, invitation_token=None):
        ident = self._get(db, email)
        if not ident or not crypto.verify_password(password, ident.password_hash):
            return AuthStep("invalid")
        if ident.disabled:
            return AuthStep("disabled")
        if ident.must_change_password:
            return AuthStep("new_password_required", subject=ident.id)
        return self._after_password(ident, require_mfa)

    async def complete_new_password(self, db, email, state, new_password, require_mfa):
        check_password_policy(new_password)
        ident = self._get(db, email)
        if not ident or ident.disabled:
            return AuthStep("invalid")
        ident.password_hash = crypto.hash_password(new_password)
        ident.must_change_password = False
        db.commit()
        return self._after_password(ident, require_mfa)

    async def verify_mfa(self, db, email, state, code):
        ident = self._get(db, email)
        if not ident or not ident.mfa_secret or not crypto.verify_totp(ident.mfa_secret, code):
            return AuthStep("invalid")
        return AuthStep("ok", subject=ident.id, refresh_token=crypto.random_token())

    async def begin_mfa_setup(self, db, email, state):
        ident = self._get(db, email)
        secret = crypto.new_totp_secret()
        return AuthStep("mfa_setup_required", subject=ident.id, mfa_secret=secret, state={"secret": secret})

    async def finish_mfa_setup(self, db, email, state, code):
        ident = self._get(db, email)
        secret = state.get("secret")
        if not ident or not secret or not crypto.verify_totp(secret, code):
            return AuthStep("invalid")
        ident.mfa_secret, ident.mfa_enabled = secret, True
        db.commit()
        return AuthStep("ok", subject=ident.id, refresh_token=crypto.random_token())

    async def forgot_password(self, db, email):
        ident = self._get(db, email)
        if not ident:
            return None
        code = f"{secrets.randbelow(1_000_000):06d}"
        ident.reset_code_hash = crypto.hash_password(code)
        ident.reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()
        log.warning("[dev] password reset code for %s (%s console): %s", email, self.console, code)
        return code

    async def reset_password(self, db, email, code, new_password):
        check_password_policy(new_password)
        ident = self._get(db, email)
        expires = ident.reset_expires_at if ident else None
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not ident or not ident.reset_code_hash or not expires or expires < datetime.now(timezone.utc) \
                or not crypto.verify_password(code, ident.reset_code_hash):
            raise bad_request("INVALID_CODE", "Incorrect email or code.")
        ident.password_hash = crypto.hash_password(new_password)
        ident.must_change_password = False
        ident.reset_code_hash = None
        db.commit()

    async def reverify(self, db, email, refresh_token):
        ident = self._get(db, email)
        return bool(ident and not ident.disabled), refresh_token

    async def create_user(self, db, email, name, organization_id=None):
        from pymicroservices.app.firm_models import DevIdentity
        if self._get(db, email):
            raise ApiError(409, "EMAIL_EXISTS", "An account with this email already exists.")
        temp = "Temp-" + secrets.token_urlsafe(9) + "9a"
        ident = DevIdentity(console=self.console, email=email, password_hash=crypto.hash_password(temp))
        db.add(ident)
        db.flush()
        log.warning("[dev] temporary password for %s (%s console): %s", email, self.console, temp)
        return ident.id, temp

    async def set_enabled(self, db, email, enabled):
        ident = self._get(db, email)
        if ident:
            ident.disabled = not enabled

    async def delete_user(self, db, email, subject=None):
        ident = self._get(db, email)
        if ident:
            db.delete(ident)

    async def global_sign_out(self, db, email):
        return None


# --------------------------------------------------------------------------------------
# WorkOS provider, headless: our own sign-in screens call WorkOS from the backend. WorkOS
# owns passwords, MFA factors, email verification and password resets; people never see
# a WorkOS page. Hosted AuthKit (authorize_url/exchange_code) stays available for SSO.
# --------------------------------------------------------------------------------------
@dataclass
class HostedIdentity:
    subject: str            # WorkOS user id
    email: str
    email_verified: bool
    first_name: str | None
    last_name: str | None
    refresh_token: str | None
    session_id: str | None  # `sid` claim, used for hosted logout
    organization_id: str | None


def _jwt_claims(token: str | None) -> dict:
    """Read claims from a WorkOS access token we just received over TLS from WorkOS itself
    (the API call is the trust boundary, so no signature check is needed here)."""
    if not token or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}


class WorkOSError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("code") or body.get("error") or str(status))
        self.status, self.body = status, body
        self.code = body.get("code") or body.get("error") or ""


GRANT_PASSWORD = "password"
GRANT_EMAIL = "urn:workos:oauth:grant-type:email-verification:code"
GRANT_TOTP = "urn:workos:oauth:grant-type:mfa-totp"
GRANT_ORG = "urn:workos:oauth:grant-type:organization-selection"


class WorkOSProvider(IdentityProvider):
    hosted = False  # our own screens; the hosted redirect is kept only for SSO later

    def __init__(self, api_key: str, client_id: str, base_url: str, transport: httpx.BaseTransport | None = None):
        self.console = "any"  # one WorkOS environment serves both consoles; our tables decide the console
        self.api_key, self.client_id = api_key, client_id
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=15, transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    # HTTP ------------------------------------------------------------------------------------
    def _raw(self, method: str, path: str, **kwargs) -> dict:
        resp = self.http.request(method, path, **kwargs)
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            log.info("WorkOS %s %s -> %s %s", method, path, resp.status_code, str(body)[:300])
            raise WorkOSError(resp.status_code, body if isinstance(body, dict) else {})
        return body

    async def _api(self, method: str, path: str, **kwargs) -> dict:
        return await asyncio.to_thread(self._raw, method, path, **kwargs)

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        """Like _api, but turns WorkOS failures into a generic API error."""
        try:
            return await self._api(method, path, **kwargs)
        except WorkOSError as exc:
            raise ApiError(502 if exc.status >= 500 else 400, "IDENTITY_PROVIDER_ERROR",
                           "The sign-in service rejected the request.") from exc

    # Authentication ----------------------------------------------------------------------------
    def _ok(self, data: dict) -> AuthStep:
        claims = _jwt_claims(data.get("access_token"))
        return AuthStep("ok", subject=(data.get("user") or {}).get("id"), refresh_token=data.get("refresh_token"),
                        session_id=claims.get("sid"))

    async def _authenticate(self, grant: str, fields: dict, require_mfa: bool, organization_id: str | None) -> AuthStep:
        body = {"client_id": self.client_id, "client_secret": self.api_key, "grant_type": grant, **fields}
        try:
            data = await self._api("POST", "/user_management/authenticate", json=body)
        except WorkOSError as exc:
            return await self._from_error(exc, require_mfa, organization_id)
        return await self._after_success(data, require_mfa)

    async def _from_error(self, exc: WorkOSError, require_mfa: bool, organization_id: str | None) -> AuthStep:
        body, pat = exc.body, exc.body.get("pending_authentication_token")
        if exc.code == "email_verification_required" and pat:
            return AuthStep("email_verification_required", state={"pat": pat, "rm": require_mfa, "org": organization_id})
        if exc.code == "mfa_enrollment" and pat:
            return AuthStep("mfa_setup_required", state={"pat": pat, "uid": (body.get("user") or {}).get("id")})
        if exc.code == "mfa_challenge" and pat:
            factor = next((f for f in body.get("authentication_factors", []) if f.get("type") == "totp"), None)
            if not factor:
                return AuthStep("invalid")
            challenge = await self._call("POST", f"/auth/factors/{factor['id']}/challenge", json={})
            return AuthStep("mfa_required", state={"pat": pat, "cid": challenge["id"]})
        if exc.code == "organization_selection_required" and pat:
            orgs = {o.get("id") for o in body.get("organizations", [])}
            if organization_id in orgs:
                return await self._authenticate(GRANT_ORG, {"pending_authentication_token": pat,
                                                            "organization_id": organization_id}, require_mfa, None)
            return AuthStep("invalid")
        if exc.code in ("sso_required", "organization_authentication_methods_required"):
            return AuthStep("sso_required")
        if exc.status >= 500:
            raise ApiError(502, "IDENTITY_PROVIDER_ERROR", "The sign-in service is unavailable. Try again shortly.")
        return AuthStep("invalid")

    async def _after_success(self, data: dict, require_mfa: bool) -> AuthStep:
        """WorkOS signed them in. If they have an authenticator, or our policy requires one
        (always for admins), we add the second step ourselves before opening a session."""
        ok = self._ok(data)
        factors = await self._call("GET", f"/user_management/users/{ok.subject}/auth_factors", params={"limit": 10})
        totp = next((f for f in factors.get("data", []) if f.get("type") == "totp"), None)
        post = {"sub": ok.subject, "rt": ok.refresh_token, "sid": ok.session_id}
        if totp:
            challenge = await self._call("POST", f"/auth/factors/{totp['id']}/challenge", json={})
            return AuthStep("mfa_required", state={"post": post, "cid": challenge["id"]})
        if require_mfa:
            return AuthStep("mfa_setup_required", state={"post": post, "uid": ok.subject})
        return ok

    async def authenticate(self, db, email, password, require_mfa, organization_id=None, invitation_token=None):
        fields = {"email": email, "password": password}
        if invitation_token:
            fields["invitation_token"] = invitation_token
        return await self._authenticate(GRANT_PASSWORD, fields, require_mfa, organization_id)

    async def verify_email(self, db, email, state, code):
        return await self._authenticate(GRANT_EMAIL, {"pending_authentication_token": state["pat"], "code": code},
                                        state.get("rm", False), state.get("org"))

    async def verify_mfa(self, db, email, state, code):
        if "pat" in state:  # WorkOS asked for the code
            body = {"pending_authentication_token": state["pat"], "authentication_challenge_id": state["cid"], "code": code}
            try:
                data = await self._api("POST", "/user_management/authenticate", json={
                    "client_id": self.client_id, "client_secret": self.api_key, "grant_type": GRANT_TOTP, **body})
            except WorkOSError:
                return AuthStep("invalid")
            return self._ok(data)
        try:  # we asked for the code after WorkOS signed them in
            result = await self._api("POST", f"/auth/challenges/{state['cid']}/verify", json={"code": code})
        except WorkOSError:
            return AuthStep("invalid")
        if not result.get("valid"):
            return AuthStep("invalid")
        post = state["post"]
        return AuthStep("ok", subject=post["sub"], refresh_token=post["rt"], session_id=post["sid"])

    async def begin_mfa_setup(self, db, email, state):
        enrolled = await self._call("POST", f"/user_management/users/{state['uid']}/auth_factors",
                                    json={"type": "totp", "totp_issuer": "Lexaior", "totp_user": email})
        factor, challenge = enrolled["authentication_factor"], enrolled["authentication_challenge"]
        return AuthStep("mfa_setup_required", mfa_secret=factor["totp"]["secret"],
                        state=dict(state, cid=challenge["id"]))

    async def finish_mfa_setup(self, db, email, state, code):
        return await self.verify_mfa(db, email, state, code)

    async def complete_new_password(self, db, email, state, new_password, require_mfa):
        raise bad_request("NOT_SUPPORTED", "Set your password from your invitation email.")

    # Passwords ---------------------------------------------------------------------------------
    async def forgot_password(self, db, email):
        try:  # WorkOS emails a link to the "Password reset URL" set in the dashboard (our /reset-password page)
            await self._api("POST", "/user_management/password_reset", json={"email": email})
        except WorkOSError as exc:
            if exc.status >= 500:
                raise ApiError(502, "IDENTITY_PROVIDER_ERROR", "The sign-in service is unavailable. Try again shortly.")
        return None  # never reveal whether the email exists

    async def reset_password(self, db, email, code, new_password):
        raise bad_request("NOT_SUPPORTED", "Use the link in your reset email.")

    async def reset_password_with_token(self, token: str, new_password: str) -> dict:
        check_password_policy(new_password)
        try:
            data = await self._api("POST", "/user_management/password_reset/confirm",
                                   json={"token": token, "new_password": new_password})
        except WorkOSError as exc:
            if "strength" in exc.code or "strength" in str(exc.body.get("message", "")).lower():
                raise bad_request("WEAK_PASSWORD", "Choose a stronger password.")
            raise bad_request("INVALID_CODE", "This reset link is invalid or has expired. Request a new one.")
        return data.get("user") or {}

    # Invitations ---------------------------------------------------------------------------------
    async def invitation_by_token(self, token: str) -> dict | None:
        try:
            invitation = await self._api("GET", f"/user_management/invitations/by_token/{token}")
        except WorkOSError:
            return None
        return invitation if invitation.get("state") == "pending" else None

    async def user_by_email(self, email: str) -> dict | None:
        data = await self._call("GET", "/user_management/users", params={"email": email, "limit": 1})
        return (data.get("data") or [None])[0]

    async def accept_invitation(self, db, invitation: dict, first_name: str, last_name: str, password: str,
                                require_mfa: bool, organization_id: str | None) -> AuthStep:
        """Create the WorkOS user (the emailed invite link proves they own the address),
        then sign in with the invitation token, which also accepts the invitation."""
        check_password_policy(password)
        email = invitation["email"].lower()
        if not await self.user_by_email(email):
            try:
                await self._api("POST", "/user_management/users", json={
                    "email": email, "password": password, "first_name": first_name, "last_name": last_name,
                    "email_verified": True})
            except WorkOSError as exc:
                if "strength" in exc.code or "strength" in str(exc.body.get("message", "")).lower():
                    raise bad_request("WEAK_PASSWORD", "Choose a stronger password.")
                raise ApiError(400, "IDENTITY_PROVIDER_ERROR", "We couldn't create your account. Try again.")
        return await self.authenticate(db, email, password, require_mfa, organization_id, invitation["token"])

    async def create_organization(self, name: str) -> str:
        return (await self._call("POST", "/organizations", json={"name": name}))["id"]

    async def invite(self, email: str, organization_id: str | None) -> dict:
        body = {"email": email, "expires_in_days": 7}
        if organization_id:
            body["organization_id"] = organization_id
        return await self._call("POST", "/user_management/invitations", json=body)

    async def create_user(self, db, email, name, organization_id: str | None = None):
        # WorkOS emails the invite; its link opens the "User invitation URL" set in the
        # dashboard (our /accept-invite page). The WorkOS user id is bound at first sign-in.
        invitation = await self.invite(email, organization_id)
        log.info("WorkOS invitation %s sent to %s", invitation.get("id"), email)
        return None, None

    async def delete_user(self, db, email, subject: str | None = None):
        try:
            pending = await self._api("GET", "/user_management/invitations", params={"email": email, "limit": 100})
            for invitation in pending.get("data", []):
                if invitation.get("state") == "pending":
                    await self._api("POST", f"/user_management/invitations/{invitation['id']}/revoke")
            if subject:
                await self._api("DELETE", f"/user_management/users/{subject}")
        except WorkOSError:
            log.warning("WorkOS cleanup for %s was incomplete", email)

    # Sessions --------------------------------------------------------------------------------------
    async def reverify(self, db, email, refresh_token):
        """Refresh with WorkOS: fails if the user was deleted or their sessions revoked
        (e.g. after a password reset). Refresh tokens rotate, so the new one is returned."""
        if not refresh_token:
            return False, None
        try:
            data = await self._api("POST", "/user_management/authenticate", json={
                "client_id": self.client_id, "client_secret": self.api_key, "grant_type": "refresh_token",
                "refresh_token": refresh_token})
        except WorkOSError as exc:
            if exc.status >= 500:  # WorkOS outage: don't sign everyone out
                return True, refresh_token
            return False, None
        return True, data.get("refresh_token") or refresh_token

    def logout_url(self, session_id: str, return_to: str) -> str:
        return f"{self.base_url}/user_management/sessions/logout?" + urlencode(
            {"session_id": session_id, "return_to": return_to})

    async def set_enabled(self, db, email, enabled):
        # WorkOS has no "disabled" state; deactivation lives in our tables and ends our sessions.
        return None

    async def global_sign_out(self, db, email):
        return None

    # Hosted AuthKit (kept for per-firm SSO later) ---------------------------------------------------
    def authorize_url(self, redirect_uri: str, state: str, login_hint: str | None = None,
                      organization_id: str | None = None) -> str:
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                  "provider": "authkit", "state": state}
        if login_hint:
            params["login_hint"] = login_hint
        if organization_id:
            params["organization_id"] = organization_id
        return f"{self.base_url}/user_management/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str, ip: str | None, user_agent: str | None) -> HostedIdentity:
        body = {"client_id": self.client_id, "client_secret": self.api_key, "grant_type": "authorization_code",
                "code": code}
        if ip:
            body["ip_address"] = ip
        if user_agent:
            body["user_agent"] = user_agent[:400]
        data = await self._call("POST", "/user_management/authenticate", json=body)
        user = data.get("user") or {}
        claims = _jwt_claims(data.get("access_token"))
        return HostedIdentity(subject=user.get("id", ""), email=(user.get("email") or "").lower(),
                              email_verified=bool(user.get("email_verified")), first_name=user.get("first_name"),
                              last_name=user.get("last_name"), refresh_token=data.get("refresh_token"),
                              session_id=claims.get("sid"), organization_id=data.get("organization_id"))


_providers: dict[str, IdentityProvider] = {}


def get_provider(console: str) -> IdentityProvider:
    s = get_settings()
    if s.auth_provider == "workos":
        if "workos" not in _providers:
            _providers["workos"] = WorkOSProvider(s.workos_api_key, s.workos_client_id, s.workos_api_base)
        return _providers["workos"]
    if console not in _providers:
        _providers[console] = DevProvider(console)
    return _providers[console]
