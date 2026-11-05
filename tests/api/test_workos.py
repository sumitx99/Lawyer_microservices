"""WorkOS behind our own sign-in screens (headless). These run against a stand-in for the
WorkOS HTTP API so CI needs no network; the same flows are also checked by hand against
WorkOS staging."""
import base64
import json

import httpx
import pytest

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.firm_models import Admin, IamUser
from pymicroservices.app.security import crypto, providers
from pymicroservices.app.security.providers import WorkOSProvider
from tests.api.conftest import Api, SessionLocal

PASSWORD = "Strong-Pass-2026"


def _token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


class WorkOSStandIn:
    """Just enough of the WorkOS User Management + MFA API for our flows."""

    def __init__(self):
        self.users = {"ava@morganlaw.test": {"id": "user_ava", "password": PASSWORD, "verified": True, "factor": None},
                      "admin@morganlaw.test": {"id": "user_admin", "password": PASSWORD, "verified": True, "factor": None},
                      "priya@morganlaw.test": {"id": "user_priya", "password": PASSWORD, "verified": False, "factor": None}}
        self.invitations = {"inv-tok": {"id": "invitation_1", "token": "inv-tok", "email": "nina@morganlaw.test",
                                        "state": "pending"}}
        self.challenges: dict[str, str] = {}  # challenge id -> factor secret
        self.calls: list[tuple[str, str, dict]] = []
        self.refresh_ok = True

    def _user(self, uid):
        return next((e, u) for e, u in self.users.items() if u["id"] == uid)

    def _success(self, email):
        u = self.users[email]
        return httpx.Response(200, json={"user": {"id": u["id"], "email": email, "email_verified": True},
                                         "access_token": _token({"sid": f"session_{u['id']}"}), "refresh_token": "rt-1"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path, method = request.url.path, request.method
        self.calls.append((method, path, body))
        assert request.headers["authorization"] == "Bearer sk_test_fake"
        if path == "/user_management/authenticate":
            grant = body["grant_type"]
            if grant == "password":
                u = self.users.get(body["email"])
                if not u or u["password"] != body["password"]:
                    return httpx.Response(400, json={"code": "invalid_credentials", "message": "Invalid"})
                if not u["verified"]:
                    return httpx.Response(403, json={"code": "email_verification_required", "pending_authentication_token": f"pat-{body['email']}",
                                                     "email": body["email"], "email_verification_id": "ev_1"})
                if body.get("invitation_token"):
                    self.invitations[body["invitation_token"]]["state"] = "accepted"
                return self._success(body["email"])
            if grant.endswith("email-verification:code"):
                email = body["pending_authentication_token"].removeprefix("pat-")
                if body["code"] != "123456":
                    return httpx.Response(400, json={"code": "invalid_one_time_code"})
                self.users[email]["verified"] = True
                return self._success(email)
            if grant == "refresh_token":
                return httpx.Response(200, json={"refresh_token": "rt-2"}) if self.refresh_ok else \
                    httpx.Response(400, json={"error": "invalid_grant"})
        if path.startswith("/user_management/users/") and path.endswith("/auth_factors"):
            uid = path.split("/")[3]
            email, u = self._user(uid)
            if method == "GET":
                return httpx.Response(200, json={"data": [{"id": f"factor_{uid}", "type": "totp"}] if u["factor"] else []})
            u["factor"] = crypto.new_totp_secret()
            self.challenges[f"ch_{uid}"] = u["factor"]
            return httpx.Response(201, json={"authentication_factor": {"id": f"factor_{uid}", "totp": {"secret": u["factor"]}},
                                             "authentication_challenge": {"id": f"ch_{uid}"}})
        if path.startswith("/auth/factors/") and path.endswith("/challenge"):
            uid = path.split("/")[3].removeprefix("factor_")
            self.challenges[f"ch_{uid}"] = self._user(uid)[1]["factor"]
            return httpx.Response(201, json={"id": f"ch_{uid}"})
        if path.startswith("/auth/challenges/") and path.endswith("/verify"):
            secret = self.challenges[path.split("/")[3]]
            return httpx.Response(200, json={"valid": crypto.verify_totp(secret, body["code"])})
        if path.startswith("/user_management/invitations/by_token/"):
            inv = self.invitations.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=inv) if inv else httpx.Response(404, json={})
        if path == "/user_management/invitations" and method == "POST":
            return httpx.Response(201, json={"id": "invitation_2", "state": "pending", "email": body["email"]})
        if path == "/user_management/users" and method == "GET":
            email = request.url.params.get("email")
            return httpx.Response(200, json={"data": [{"id": self.users[email]["id"]}] if email in self.users else []})
        if path == "/user_management/users" and method == "POST":
            self.users[body["email"]] = {"id": "user_new", "password": body["password"], "verified": True, "factor": None}
            return httpx.Response(201, json={"id": "user_new", "email": body["email"]})
        if path == "/user_management/password_reset":
            return httpx.Response(201, json={"id": "pr_1"})
        if path == "/user_management/password_reset/confirm":
            if body["token"] != "reset-tok":
                return httpx.Response(400, json={"code": "password_reset_token_invalid"})
            self.users["ava@morganlaw.test"]["password"] = body["new_password"]
            return httpx.Response(200, json={"user": {"id": "user_ava", "email": "ava@morganlaw.test"}})
        return httpx.Response(404, json={"code": "not_found"})


@pytest.fixture
def workos(monkeypatch):
    fake = WorkOSStandIn()
    settings = get_settings()
    for key, value in {"auth_provider": "workos", "workos_api_key": "sk_test_fake", "workos_client_id": "client_fake",
                       "app_url": "http://testserver", "admin_require_mfa": True}.items():
        monkeypatch.setattr(settings, key, value)
    provider = WorkOSProvider("sk_test_fake", "client_fake", "https://api.workos.test", transport=httpx.MockTransport(fake))
    monkeypatch.setattr(providers, "_providers", {"workos": provider})
    with SessionLocal() as db:  # seeded accounts carry dev-provider ids; WorkOS binds its own on first sign-in
        db.query(IamUser).update({"provider_subject": None})
        db.query(Admin).update({"provider_subject": None})
        db.commit()
    return fake


def _login(api: Api, console: str, email: str, password: str = PASSWORD):
    return api.c.post(f"/api/auth/{console}/login", json={"email": email, "password": password})


def test_config_reports_our_own_screens(workos):
    assert Api().c.get("/api/auth/config").json()["mode"] == "password"


def test_password_sign_in_on_our_page(workos):
    api = Api("iam")
    r = _login(api, "iam", "ava@morganlaw.test")
    assert r.json()["status"] == "success", r.text
    assert api.get("/api/me").json()["principal"]["role"] == "ATTORNEY"
    with SessionLocal() as db:
        assert db.query(IamUser).filter_by(email="ava@morganlaw.test").one().provider_subject == "user_ava"
    assert _login(Api(), "iam", "ava@morganlaw.test", "wrong").json()["code"] == "INVALID_CREDENTIALS"


def test_admin_must_set_up_mfa_then_enter_codes(workos):
    api = Api("admin")
    r = _login(api, "admin", "admin@morganlaw.test").json()
    assert r["status"] == "MFA_SETUP_REQUIRED"
    setup = api.c.post("/api/auth/admin/mfa/setup", json={"challenge": r["challenge"]}).json()
    bad = api.c.post("/api/auth/admin/mfa/setup/verify", json={"challenge": setup["challenge"], "code": "000000"})
    assert bad.status_code == 401
    ok = api.c.post("/api/auth/admin/mfa/setup/verify",
                    json={"challenge": setup["challenge"], "code": crypto.totp(setup["secret"])}).json()
    assert ok["status"] == "success"
    # Next time: code only
    again = Api("admin")
    r = _login(again, "admin", "admin@morganlaw.test").json()
    assert r["status"] == "MFA_REQUIRED"
    secret = workos.users["admin@morganlaw.test"]["factor"]
    assert again.c.post("/api/auth/admin/mfa/verify", json={"challenge": r["challenge"], "code": crypto.totp(secret)}).json()["status"] == "success"


def test_unverified_email_gets_a_code_step(workos):
    api = Api("iam")
    r = _login(api, "iam", "priya@morganlaw.test").json()
    assert r["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert api.c.post("/api/auth/iam/email/verify", json={"challenge": r["challenge"], "code": "999999"}).status_code == 401
    assert api.c.post("/api/auth/iam/email/verify", json={"challenge": r["challenge"], "code": "123456"}).json()["status"] == "success"


def test_accept_invitation_on_our_page(workos):
    admin = Api("admin")
    with SessionLocal() as db:  # admin invited nina earlier
        firm_id = db.query(Admin).first().firm_id
        db.add(IamUser(firm_id=firm_id, email="nina@morganlaw.test", name="Nina", firm_role="PARALEGAL", status="invited"))
        db.commit()
    info = admin.c.get("/api/auth/invitations/inv-tok").json()
    assert info["email"] == "nina@morganlaw.test" and info["console"] == "iam" and info["has_account"] is False
    nina = Api("iam")
    weak = nina.c.post("/api/auth/iam/invite/accept", json={"token": "inv-tok", "first_name": "Nina", "password": "short"})
    assert weak.json()["code"] == "WEAK_PASSWORD"
    r = nina.c.post("/api/auth/iam/invite/accept", json={"token": "inv-tok", "first_name": "Nina", "last_name": "Lopez",
                                                        "password": "Nina-Strong-2026"}).json()
    assert r["status"] == "success" and r["principal"]["name"] == "Nina Lopez" and r["principal"]["status"] == "active"
    assert workos.invitations["inv-tok"]["state"] == "accepted"
    assert Api().c.get("/api/auth/invitations/inv-tok").status_code == 404  # used


def test_reset_password_with_emailed_token(workos):
    old = Api("iam")
    _login(old, "iam", "ava@morganlaw.test")
    api = Api("iam")
    assert api.c.post("/api/auth/iam/forgot", json={"email": "ava@morganlaw.test"}).json()["status"] == "success"
    assert ("POST", "/user_management/password_reset", {"email": "ava@morganlaw.test"}) in workos.calls
    assert api.c.post("/api/auth/reset-password", json={"token": "bad-token", "new_password": "Brand-New-Pass-2026"}).json()["code"] == "INVALID_CODE"
    assert api.c.post("/api/auth/reset-password", json={"token": "reset-tok", "new_password": "Brand-New-Pass-2026"}).status_code == 200
    assert old.get("/api/me").status_code == 401  # our sessions end too
    assert _login(Api(), "iam", "ava@morganlaw.test", "Brand-New-Pass-2026").json()["status"] == "success"


def test_session_recheck_rotates_and_revokes(workos, monkeypatch):
    monkeypatch.setattr(get_settings(), "session_reverify_minutes", 0)
    api = Api("iam")
    _login(api, "iam", "ava@morganlaw.test")
    assert api.get("/api/me").status_code == 200
    assert any(body.get("refresh_token") == "rt-1" for _, _, body in workos.calls)
    workos.refresh_ok = False  # e.g. user deleted in WorkOS
    assert api.get("/api/me").status_code == 401


def test_admin_invite_goes_through_workos(workos):
    admin = Api("admin")
    r = _login(admin, "admin", "admin@morganlaw.test").json()
    setup = admin.c.post("/api/auth/admin/mfa/setup", json={"challenge": r["challenge"]}).json()
    admin.c.post("/api/auth/admin/mfa/setup/verify", json={"challenge": setup["challenge"], "code": crypto.totp(setup["secret"])})
    r = admin.post("/api/admin/iam-users", {"email": "omar@morganlaw.test", "name": "Omar", "firm_role": "PARALEGAL"})
    assert r.status_code == 201 and "dev_temporary_password" not in r.json()
    assert ("POST", "/user_management/invitations", {"email": "omar@morganlaw.test", "expires_in_days": 7}) in workos.calls
