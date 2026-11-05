"""Local demo data: one firm, one admin, one IAM user per role, a team and two matters.
Every account's password is DEV_PASSWORD and MFA is off, so you can try each role fast.

    python -m scripts.seed_dev            (AUTH_PROVIDER=dev only)
"""
from sqlalchemy import select

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.utilities.firm_db import SessionLocal
from pymicroservices.app.firm_models import (Admin, Collection, DevIdentity, Firm, IamUser, Matter, MatterCollection, MatterMember, Team,
                        TeamMember)
from pymicroservices.app.security import crypto
from pymicroservices.app.security.permissions import sync_permission_sets

DEV_PASSWORD = "Lexaior-dev-2026"
PEOPLE = [
    ("MANAGING_ATTORNEY", "Maya Patel", "maya@morganlaw.test"),
    ("FIRM_VIEWER", "Felix Grant", "felix@morganlaw.test"),
    ("ATTORNEY", "Ava Chen", "ava@morganlaw.test"),
    ("CASE_MANAGER", "Carlos Ruiz", "carlos@morganlaw.test"),
    ("PARALEGAL", "Priya Shah", "priya@morganlaw.test"),
    ("PARALEGAL", "Ben Ortiz", "ben@morganlaw.test"),
]


def ident(db, console: str, email: str) -> str:
    row = DevIdentity(console=console, email=email, password_hash=crypto.hash_password(DEV_PASSWORD),
                      must_change_password=False)
    db.add(row)
    db.flush()
    return row.id


def main() -> None:
    if not get_settings().dev_auth:
        raise SystemExit("seed_dev only runs with AUTH_PROVIDER=dev")
    with SessionLocal() as db:
        sync_permission_sets(db)
        if db.scalar(select(Firm).where(Firm.slug == "morgan-law")):
            print("Already seeded.")
            return
        firm = Firm(name="Morgan Injury Law (demo)", slug="morgan-law", require_mfa=False,
                    allowed_email_domains=["morganlaw.test"])
        db.add(firm)
        db.flush()
        db.add(Admin(firm_id=firm.id, email="admin@morganlaw.test", name="Alex Admin", status="active",
                     provider_subject=ident(db, "admin", "admin@morganlaw.test")))
        users = {}
        for role, name, email in PEOPLE:
            u = IamUser(firm_id=firm.id, email=email, name=name, firm_role=role, status="active",
                        provider_subject=ident(db, "iam", email))
            db.add(u)
            db.flush()
            users[email] = u
        team = Team(firm_id=firm.id, name="PI Team A", description="Pre-litigation personal injury")
        db.add(team)
        db.flush()
        for email in ("priya@morganlaw.test", "carlos@morganlaw.test"):
            db.add(TeamMember(team_id=team.id, user_id=users[email].id))

        m1 = Matter(firm_id=firm.id, number="M-0001", name="Morgan v. Westfield", client_name="Jordan Morgan",
                    created_by=users["ava@morganlaw.test"].id)
        m2 = Matter(firm_id=firm.id, number="M-0002", name="Reyes v. Harbor Transit", client_name="Lena Reyes",
                    created_by=users["carlos@morganlaw.test"].id)
        db.add_all([m1, m2])
        db.flush()
        db.add_all([
            MatterMember(matter_id=m1.id, principal_type="user", principal_id=users["ava@morganlaw.test"].id, role="LEAD"),
            MatterMember(matter_id=m1.id, principal_type="team", principal_id=team.id, role="EDITOR"),
            MatterMember(matter_id=m1.id, principal_type="user", principal_id=users["ben@morganlaw.test"].id,
                         role="BLOCKED", wall_reason="Previously represented the defendant's insurer"),
            MatterMember(matter_id=m2.id, principal_type="user", principal_id=users["carlos@morganlaw.test"].id, role="LEAD"),
            MatterMember(matter_id=m2.id, principal_type="user", principal_id=users["ben@morganlaw.test"].id, role="VIEWER"),
        ])
        c1 = Collection(firm_id=firm.id, name="Medical records", description="ED, PT and imaging",
                        created_by=users["ava@morganlaw.test"].id)
        db.add(c1)
        db.flush()
        db.add(MatterCollection(matter_id=m1.id, collection_id=c1.id))
        db.commit()
        print("Seeded firm 'Morgan Injury Law (demo)'. Password for every account:", DEV_PASSWORD)
        print("  Admin console: admin@morganlaw.test")
        for role, name, email in PEOPLE:
            print(f"  IAM {role:<18} {email}")


if __name__ == "__main__":
    main()
