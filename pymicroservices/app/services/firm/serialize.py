"""Response shapes shared by several routers."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from pymicroservices.app.firm_models import Admin, Firm, IamUser, Matter, MatterCollection, Team, TeamMember
from pymicroservices.app.security import permissions as P


def iso(value):
    return value.isoformat() if value else None


def principal_out(db: Session, console: str, person) -> dict:
    firm = db.get(Firm, person.firm_id)
    out = {"console": console, "id": person.id, "email": person.email, "name": person.name,
           "status": person.status, "firm": {"id": firm.id, "name": firm.name}}
    if console == "admin":
        out.update(role="ADMIN", role_name="Firm Admin", capabilities={"admin": True, "matter_create": True,
                                                                     "collection_create": True, "user_picker": True})
    else:
        perms = P.firm_permissions(person.firm_role)
        spec = P.FIRM_ROLES.get(person.firm_role, {})
        out.update(role=person.firm_role, role_name=spec.get("name", person.firm_role),
                   ceiling=bool(spec.get("ceiling")), licensed=bool(spec.get("licensed")),
                   capabilities={flag: perm in perms for perm, flag in P.FIRM_CAPABILITY_FLAGS.items()})
    return out


def user_out(db: Session, user: IamUser, with_teams: bool = True) -> dict:
    out = {"id": user.id, "email": user.email, "name": user.name, "firm_role": user.firm_role,
           "firm_role_name": P.FIRM_ROLES.get(user.firm_role, {}).get("name", user.firm_role),
           "status": user.status, "last_login_at": iso(user.last_login_at), "created_at": iso(user.created_at)}
    if with_teams:
        out["teams"] = [{"id": t.id, "name": t.name} for t in db.scalars(
            select(Team).join(TeamMember, TeamMember.team_id == Team.id).where(TeamMember.user_id == user.id))]
    return out


def admin_out(admin: Admin) -> dict:
    return {"id": admin.id, "email": admin.email, "name": admin.name, "status": admin.status,
            "admin_role": admin.admin_role, "last_login_at": iso(admin.last_login_at), "created_at": iso(admin.created_at)}


def matter_out(matter: Matter, **extra) -> dict:
    return {"id": matter.id, "number": matter.number, "name": matter.name, "client_name": matter.client_name,
            "practice_area": matter.practice_area, "status": matter.status, "opened_at": iso(matter.opened_at),
            "closed_at": iso(matter.closed_at), **extra}


def collection_matters(db: Session, collection_ids: list[str], visible_ids: set[str] | None) -> dict[str, list]:
    if not collection_ids:
        return {}
    rows = db.execute(select(MatterCollection.collection_id, Matter.id, Matter.name, Matter.number)
                      .join(Matter, Matter.id == MatterCollection.matter_id)
                      .where(MatterCollection.collection_id.in_(collection_ids), Matter.deleted_at.is_(None)))
    out: dict[str, list] = {}
    for cid, mid, name, number in rows:
        if visible_ids is None or mid in visible_ids:
            out.setdefault(cid, []).append({"id": mid, "name": name, "number": number})
    return out
