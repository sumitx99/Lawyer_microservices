"""Role matrix against the seeded firm:
  Maya   MANAGING_ATTORNEY (ceiling)     Felix FIRM_VIEWER (ceiling, read-only)
  Ava    ATTORNEY, Lead on Morgan        Carlos CASE_MANAGER, Lead on Reyes, in PI Team A
  Priya  PARALEGAL, in PI Team A (Editor on Morgan via the team)
  Ben    PARALEGAL, walled off Morgan, Viewer on Reyes"""
from sqlalchemy import select

from pymicroservices.app.firm_models import Firm, IamUser, Matter, MatterMember
from pymicroservices.app.security import access
from pymicroservices.app.security.access import Principal
from tests.api.conftest import as_admin, as_user, in_db, matter_id, user_id


def names(api) -> set[str]:
    return {m["name"] for m in api.get("/api/matters").json()["items"]}


def test_matter_lists_follow_roles_and_walls():
    assert names(as_user("maya@morganlaw.test")) == {"Morgan v. Westfield", "Reyes v. Harbor Transit"}
    assert names(as_user("felix@morganlaw.test")) == {"Morgan v. Westfield", "Reyes v. Harbor Transit"}
    assert names(as_user("ava@morganlaw.test")) == {"Morgan v. Westfield"}
    assert names(as_user("priya@morganlaw.test")) == {"Morgan v. Westfield"}      # through her team
    assert names(as_user("ben@morganlaw.test")) == {"Reyes v. Harbor Transit"}    # walled off Morgan
    assert names(as_admin()) == {"Morgan v. Westfield", "Reyes v. Harbor Transit"}


def test_wall_returns_404_for_matter_and_its_collections():
    ben = as_user("ben@morganlaw.test")
    morgan = matter_id("Morgan v. Westfield")
    assert ben.get(f"/api/matters/{morgan}").status_code == 404
    assert ben.get("/api/collections").json()["items"] == []
    priya = as_user("priya@morganlaw.test")
    col = priya.get(f"/api/matters/{morgan}/collections").json()["items"][0]["id"]
    assert ben.get(f"/api/collections/{col}").status_code == 404


def test_create_matter_needs_firm_role():
    body = {"name": "Kim v. Rapid Freight", "client_name": "Dae Kim"}
    assert as_user("priya@morganlaw.test").post("/api/matters", body).status_code == 403
    assert as_user("felix@morganlaw.test").post("/api/matters", body).status_code == 403
    r = as_user("carlos@morganlaw.test").post("/api/matters", body)
    assert r.status_code == 201 and r.json()["matter"]["my_role"] == "LEAD"


def test_admin_creating_matter_must_name_a_lead():
    admin = as_admin()
    assert admin.post("/api/matters", {"name": "A", "client_name": "B"}).json()["code"] == "LEAD_REQUIRED"
    r = admin.post("/api/matters", {"name": "A v. B", "client_name": "B", "lead_user_id": user_id("ava@morganlaw.test")})
    assert r.status_code == 201
    assert "A v. B" in names(as_user("ava@morganlaw.test"))


def test_viewer_and_firm_viewer_are_read_only():
    reyes = matter_id("Reyes v. Harbor Transit")
    for email in ("ben@morganlaw.test", "felix@morganlaw.test"):
        api = as_user(email)
        assert api.get(f"/api/matters/{reyes}").status_code == 200
        assert api.patch(f"/api/matters/{reyes}", {"name": "Changed"}).status_code == 403
        assert api.post(f"/api/matters/{reyes}/collections", {"name": "x"}).status_code == 403


def test_attorney_only_permission_needs_licensed_firm_role():
    """A Paralegal made Lead still can't approve drafts; an Attorney Lead can."""
    morgan = matter_id("Morgan v. Westfield")

    async def check(db):
        priya = await db.scalar(select(IamUser).where(IamUser.email == "priya@morganlaw.test"))
        db.add(MatterMember(matter_id=morgan, principal_type="user", principal_id=priya.id, role="LEAD"))
        await db.flush()
        p = Principal("iam", priya.id, priya.firm_id, priya.email, priya.name, priya.firm_role)
        perms, role, _ = await access.matter_perms(db, p, morgan)
        assert role == "LEAD" and "members.manage" in perms and "drafts.approve" not in perms
        ava = await db.scalar(select(IamUser).where(IamUser.email == "ava@morganlaw.test"))
        a = Principal("iam", ava.id, ava.firm_id, ava.email, ava.name, ava.firm_role)
        assert "drafts.approve" in (await access.matter_perms(db, a, morgan))[0]
    in_db(check)


def test_editor_can_only_grant_viewer():
    morgan = matter_id("Morgan v. Westfield")
    priya = as_user("priya@morganlaw.test")  # Editor via team
    felix = user_id("felix@morganlaw.test")
    r = priya.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": felix, "action": "assign", "role": "EDITOR"}]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_MEMBER_CHANGES"
    r = priya.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": felix, "action": "assign", "role": "VIEWER"}]})
    assert r.status_code == 200, r.text


def test_last_lead_needs_handover_and_concurrency_check():
    morgan = matter_id("Morgan v. Westfield")
    ava = as_user("ava@morganlaw.test")
    me = user_id("ava@morganlaw.test")
    r = ava.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": me, "action": "remove", "expected_role": "LEAD"}]})
    assert r.status_code == 409 and r.json()["code"] == "NEEDS_HANDOVER"
    r = ava.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": me, "action": "remove", "expected_role": "EDITOR"}]})
    assert r.status_code == 409 and r.json()["code"] == "MEMBERS_CHANGED"
    r = ava.post(f"/api/matters/{morgan}/members", {
        "changes": [{"principal_type": "user", "principal_id": me, "action": "remove", "expected_role": "LEAD"}],
        "new_lead_user_id": user_id("maya@morganlaw.test")})
    assert r.status_code == 200, r.text
    assert "Morgan v. Westfield" not in names(ava)


def test_wall_needs_reason_and_hides_from_non_managers():
    morgan = matter_id("Morgan v. Westfield")
    ava = as_user("ava@morganlaw.test")
    carlos = user_id("carlos@morganlaw.test")
    r = ava.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": carlos, "action": "assign", "role": "BLOCKED"}]})
    assert r.json()["code"] == "INVALID_MEMBER_CHANGES"
    r = ava.post(f"/api/matters/{morgan}/members", {"changes": [
        {"principal_type": "user", "principal_id": carlos, "action": "assign", "role": "BLOCKED",
         "wall_reason": "Conflict check 2026-10"}]})
    assert r.status_code == 200
    # Carlos is in PI Team A (Editor on Morgan) but the wall wins
    assert "Morgan v. Westfield" not in names(as_user("carlos@morganlaw.test"))
    priya_view = as_user("priya@morganlaw.test").get(f"/api/matters/{morgan}/members").json()["members"]
    assert all(m["role"] != "BLOCKED" for m in priya_view)


def test_admin_ignores_walls():
    admin = as_admin()
    morgan = matter_id("Morgan v. Westfield")
    members = admin.get(f"/api/matters/{morgan}/members").json()["members"]
    assert any(m["role"] == "BLOCKED" and m["wall_reason"] for m in members)


def test_firms_are_isolated():
    async def other_firm(db):
        other = Firm(name="Other Firm", slug="other")
        db.add(other)
        await db.flush()
        db.add(Matter(firm_id=other.id, number="M-0001", name="Secret v. Other", client_name="Z"))
    in_db(other_firm)
    secret = matter_id("Secret v. Other")
    for api in (as_admin(), as_user("maya@morganlaw.test")):
        assert "Secret v. Other" not in names(api)
        assert api.get(f"/api/matters/{secret}").status_code == 404


def test_user_picker_is_limited_for_non_ceiling_roles():
    ben = {u["email"] for u in as_user("ben@morganlaw.test").get("/api/users/picker").json()["users"]}
    assert "maya@morganlaw.test" not in ben and "carlos@morganlaw.test" in ben  # shares Reyes with Carlos
    maya = {u["email"] for u in as_user("maya@morganlaw.test").get("/api/users/picker").json()["users"]}
    assert "ben@morganlaw.test" in maya
