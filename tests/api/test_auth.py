from pymicroservices.app.firm_models import Firm
from pymicroservices.app.security import crypto
from tests.api.conftest import Api, SessionLocal, as_admin, as_user


def test_every_route_is_guarded():
    from pymicroservices.app.main import create_app
    create_app()  # raises if any /api route has no guard


def test_invite_first_login_new_password_and_mfa_setup():
    with SessionLocal() as db:
        db.query(Firm).update({"require_mfa": True})
        db.commit()
    admin = as_admin()
    r = admin.post("/api/admin/iam-users", {"email": "nina@morganlaw.test", "name": "Nina Lopez",
                                            "firm_role": "PARALEGAL"})
    assert r.status_code == 201, r.text
    temp = r.json()["dev_temporary_password"]

    nina = Api("iam")
    r = nina.c.post("/api/auth/iam/login", json={"email": "nina@morganlaw.test", "password": temp})
    assert r.json()["status"] == "NEW_PASSWORD_REQUIRED"
    weak = nina.c.post("/api/auth/iam/new-password", json={"challenge": r.json()["challenge"], "new_password": "short"})
    assert weak.status_code == 400 and weak.json()["code"] == "WEAK_PASSWORD"
    r = nina.c.post("/api/auth/iam/new-password", json={"challenge": r.json()["challenge"],
                                                        "new_password": "Nina-Strong-2026"})
    assert r.json()["status"] == "MFA_SETUP_REQUIRED"
    setup = nina.c.post("/api/auth/iam/mfa/setup", json={"challenge": r.json()["challenge"]}).json()
    code = crypto.totp(setup["secret"])
    r = nina.c.post("/api/auth/iam/mfa/setup/verify", json={"challenge": setup["challenge"], "code": code})
    assert r.json()["status"] == "success", r.text
    assert nina.get("/api/me").json()["principal"]["role"] == "PARALEGAL"

    # Next login asks for the MFA code
    again = Api("iam")
    r = again.c.post("/api/auth/iam/login", json={"email": "nina@morganlaw.test", "password": "Nina-Strong-2026"})
    assert r.json()["status"] == "MFA_REQUIRED"
    bad = again.c.post("/api/auth/iam/mfa/verify", json={"challenge": r.json()["challenge"], "code": "000000"})
    assert bad.status_code == 401


def test_wrong_password_is_generic_and_rate_limited():
    api = Api("iam")
    for _ in range(5):
        r = api.c.post("/api/auth/iam/login", json={"email": "ava@morganlaw.test", "password": "nope"})
        assert r.status_code == 401 and r.json()["code"] == "INVALID_CREDENTIALS"
    r = api.c.post("/api/auth/iam/login", json={"email": "ava@morganlaw.test", "password": "nope"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_consoles_are_separate():
    ava = as_user("ava@morganlaw.test")
    assert ava.get("/api/admin/iam-users").status_code == 403          # IAM user, admin route
    ava.console = "admin"
    assert ava.get("/api/admin/iam-users").status_code == 401          # no admin session cookie
    # An IAM password doesn't work on the admin console
    r = Api("admin").c.post("/api/auth/admin/login", json={"email": "ava@morganlaw.test", "password": "Lexaior-dev-2026"})
    assert r.status_code == 401
    admin = as_admin()
    assert admin.get("/api/admin/iam-users").status_code == 200


def test_csrf_required_on_changes():
    ava = as_user("ava@morganlaw.test")
    r = ava.c.post("/api/matters", json={"name": "X", "client_name": "Y"}, headers={"x-csrf-token": "wrong"})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"


def test_logout_and_deactivation_end_sessions_immediately():
    ava = as_user("ava@morganlaw.test")
    assert ava.get("/api/me").status_code == 200
    ava.post("/api/auth/iam/logout")
    assert ava.get("/api/me").status_code == 401

    priya = as_user("priya@morganlaw.test")
    admin = as_admin()
    pid = priya.get("/api/me").json()["principal"]["id"]
    assert admin.post(f"/api/admin/iam-users/{pid}/deactivate").status_code == 200
    assert priya.get("/api/me").status_code == 401
    r = Api("iam").c.post("/api/auth/iam/login", json={"email": "priya@morganlaw.test", "password": "Lexaior-dev-2026"})
    assert r.status_code == 401


def test_forgot_and_reset_password_revokes_sessions():
    old = as_user("ava@morganlaw.test")
    api = Api("iam")
    code = api.c.post("/api/auth/iam/forgot", json={"email": "ava@morganlaw.test"}).json()["dev_code"]
    r = api.c.post("/api/auth/iam/reset", json={"email": "ava@morganlaw.test", "code": code,
                                                "new_password": "Ava-New-Pass-2026"})
    assert r.status_code == 200, r.text
    assert old.get("/api/me").status_code == 401
    Api("iam").login("ava@morganlaw.test", "Ava-New-Pass-2026")


def test_admin_cannot_remove_last_admin_or_self():
    admin = as_admin()
    me = admin.get("/api/me").json()["principal"]["id"]
    assert admin.post(f"/api/admin/admins/{me}/deactivate").json()["code"] == "SELF"
