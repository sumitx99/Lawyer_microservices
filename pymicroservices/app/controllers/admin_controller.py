"""Admin console API (/api/admin). Every route: require_admin() -> Admin console session.
Admins also use the shared matter/collection APIs (with X-Console: admin), where they pass
every check."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pymicroservices.app.utilities.validators import Email
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import ApiError, bad_request, conflict, not_found
from pymicroservices.app.firm_models import (Admin, AuditEvent, AuthSession, Collection, Firm, IamUser, Matter, MatterMember,
                        OwnershipTransfer, Team, TeamMember)
from pymicroservices.app.security import permissions as P
from pymicroservices.app.security import sessions
from pymicroservices.app.security.guard import Ctx, require_admin
from pymicroservices.app.security.providers import get_provider
from pymicroservices.app.services.collections import collection_files
from pymicroservices.app.services.firm import audit
from pymicroservices.app.services.firm.serialize import admin_out, iso, user_out

router = APIRouter(prefix="/api/admin", tags=["admin"])
ADMIN = Depends(require_admin())


async def _user(db: AsyncSession, ctx: Ctx, user_id: str) -> IamUser:
    user = await db.get(IamUser, user_id)
    if not user or user.firm_id != ctx.who.firm_id:
        raise not_found("User")
    return user


async def _check_domain(db: AsyncSession, ctx: Ctx, email: str) -> None:
    firm = await db.get(Firm, ctx.who.firm_id)
    domains = [d.lower().lstrip("@") for d in (firm.allowed_email_domains or [])]
    if domains and email.split("@")[-1] not in domains:
        raise bad_request("EMAIL_DOMAIN_NOT_ALLOWED", f"Only {', '.join(domains)} email addresses can be invited.")


async def _set_teams(db: AsyncSession, ctx: Ctx, user_id: str, team_ids: list[str]) -> None:
    valid = set(await db.scalars(select(Team.id).where(Team.firm_id == ctx.who.firm_id, Team.id.in_(team_ids or []))))
    if set(team_ids or []) - valid:
        raise bad_request("UNKNOWN_TEAM", "One of the teams doesn't exist.")
    await db.execute(delete(TeamMember).where(TeamMember.user_id == user_id))
    for tid in valid:
        db.add(TeamMember(team_id=tid, user_id=user_id))


# Overview ----------------------------------------------------------------------------
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    fid = ctx.who.firm_id
    by_role = dict((await db.execute(select(IamUser.firm_role, func.count()).where(IamUser.firm_id == fid)
                                     .group_by(IamUser.firm_role))).all())
    by_status = dict((await db.execute(select(IamUser.status, func.count()).where(IamUser.firm_id == fid)
                                       .group_by(IamUser.status))).all())
    matters = dict((await db.execute(select(Matter.status, func.count()).where(Matter.firm_id == fid, Matter.deleted_at.is_(None))
                                     .group_by(Matter.status))).all())
    docs = await collection_files.firm_document_statuses(db, fid)
    recent = await db.scalars(select(AuditEvent).where(AuditEvent.firm_id == fid).order_by(AuditEvent.id.desc()).limit(8))
    return {"users_by_role": by_role, "users_by_status": by_status, "matters_by_status": matters,
            "documents_by_status": docs,
            "collections": await db.scalar(select(func.count()).select_from(Collection)
                                     .where(Collection.firm_id == fid, Collection.deleted_at.is_(None))),
            "teams": await db.scalar(select(func.count()).select_from(Team).where(Team.firm_id == fid)),
            "recent_activity": [_audit_out(db, e) for e in recent]}


# Roles (read-only matrix) ---------------------------------------------------------------
@router.get("/roles")
def roles(ctx: Ctx = ADMIN):
    return {"firm_roles": [{"key": k, **{f: v[f] for f in ("name", "ceiling", "licensed")},
                            "permissions": sorted(set(v["permissions"]))} for k, v in P.FIRM_ROLES.items()],
            "matter_roles": [{"key": k, "name": v["name"], "permissions": sorted(set(v["permissions"])),
                              "can_grant": list(P.GRANTS_BY_MATTER_ROLE[k])} for k, v in P.MATTER_ROLES.items()],
            "permissions": P.ALL_PERMISSIONS}


# IAM users ------------------------------------------------------------------------------
class InviteUserIn(BaseModel):
    email: Email
    name: str = Field(min_length=1, max_length=200)
    firm_role: str
    team_ids: list[str] = []


class UpdateUserIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    firm_role: str | None = None
    team_ids: list[str] | None = None


@router.get("/iam-users")
async def list_users(q: str = "", role: str = "", status: str = "", db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    stmt = select(IamUser).where(IamUser.firm_id == ctx.who.firm_id)
    if q:
        stmt = stmt.where(or_(IamUser.name.ilike(f"%{q}%"), IamUser.email.ilike(f"%{q}%")))
    if role:
        stmt = stmt.where(IamUser.firm_role == role)
    if status:
        stmt = stmt.where(IamUser.status == status)
    return {"items": [await user_out(db, u) for u in await db.scalars(stmt.order_by(IamUser.name))]}


@router.post("/iam-users", status_code=201)
async def invite_user(body: InviteUserIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    if body.firm_role not in P.FIRM_ROLES:
        raise bad_request("UNKNOWN_ROLE", "Unknown role.")
    email = body.email.lower()
    await _check_domain(db, ctx, email)
    if await db.scalar(select(IamUser).where(IamUser.email == email)):
        raise conflict("EMAIL_EXISTS", "A user with this email already exists.")
    provider = get_provider("iam")
    subject, temp_password = await provider.create_user(db, email, body.name.strip(),
                                                     organization_id=(await db.get(Firm, ctx.who.firm_id)).workos_organization_id)
    try:
        user = IamUser(firm_id=ctx.who.firm_id, email=email, name=body.name.strip(), firm_role=body.firm_role,
                       status="invited", provider_subject=subject, created_by_admin=ctx.who.id)
        db.add(user)
        await db.flush()
        await _set_teams(db, ctx, user.id, body.team_ids)
        audit.record_ctx(db, ctx, "user.invited", "iam_user", user.id, email=email, role=body.firm_role)
        await db.commit()
    except Exception:
        await db.rollback()
        await provider.delete_user(db, email)  # keep WorkOS and the database in step
        await db.commit()
        raise
    out = {"user": await user_out(db, user)}
    if get_settings().dev_auth and temp_password:
        out["dev_temporary_password"] = temp_password
    return out


@router.get("/iam-users/{user_id}")
async def get_user(user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    user = await _user(db, ctx, user_id)
    memberships = (await db.execute(select(Matter.id, Matter.name, Matter.number, MatterMember.role)
                                    .join(MatterMember, MatterMember.matter_id == Matter.id)
                                    .where(MatterMember.principal_type == "user", MatterMember.principal_id == user.id,
                                           Matter.deleted_at.is_(None)))).all()
    active = await db.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.console == "iam", AuthSession.principal_id == user.id, AuthSession.revoked_at.is_(None)))
    return {"user": await user_out(db, user), "active_sessions": active,
            "matters": [{"id": m, "name": n, "number": num, "role": r} for m, n, num, r in memberships]}


@router.patch("/iam-users/{user_id}")
async def update_user(user_id: str, body: UpdateUserIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    user = await _user(db, ctx, user_id)
    if body.name is not None:
        user.name = body.name.strip()
    if body.firm_role is not None and body.firm_role != user.firm_role:
        if body.firm_role not in P.FIRM_ROLES:
            raise bad_request("UNKNOWN_ROLE", "Unknown role.")
        audit.record_ctx(db, ctx, "user.role_changed", "iam_user", user.id, old=user.firm_role, new=body.firm_role)
        user.firm_role = body.firm_role
    if body.team_ids is not None:
        await _set_teams(db, ctx, user.id, body.team_ids)
        audit.record_ctx(db, ctx, "user.teams_changed", "iam_user", user.id, team_ids=body.team_ids)
    await db.commit()
    return {"user": await user_out(db, user)}


@router.post("/iam-users/{user_id}/deactivate")
async def deactivate_user(user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    user = await _user(db, ctx, user_id)
    user.status = "deactivated"
    ended = await sessions.revoke_all(db, "iam", user.id)
    provider = get_provider("iam")
    await provider.set_enabled(db, user.email, False)
    await provider.global_sign_out(db, user.email)
    audit.record_ctx(db, ctx, "user.deactivated", "iam_user", user.id, sessions_ended=ended)
    await db.commit()
    return {"user": await user_out(db, user)}


@router.post("/iam-users/{user_id}/reactivate")
async def reactivate_user(user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    user = await _user(db, ctx, user_id)
    user.status = "active" if user.last_login_at else "invited"
    await get_provider("iam").set_enabled(db, user.email, True)
    audit.record_ctx(db, ctx, "user.reactivated", "iam_user", user.id)
    await db.commit()
    return {"user": await user_out(db, user)}


async def _ownership(db: AsyncSession, user_id: str) -> dict:
    created_matters = await db.scalar(select(func.count()).select_from(Matter).where(
        Matter.created_by == user_id, Matter.deleted_at.is_(None)))
    created_collections = await db.scalar(select(func.count()).select_from(Collection).where(
        Collection.created_by == user_id, Collection.deleted_at.is_(None)))
    lead_of = await db.scalar(select(func.count()).select_from(MatterMember).where(
        MatterMember.principal_type == "user", MatterMember.principal_id == user_id, MatterMember.role == "LEAD"))
    return {"created_matters": created_matters, "created_collections": created_collections, "lead_of": lead_of,
            "can_delete": not (created_matters or created_collections or lead_of)}


@router.get("/iam-users/{user_id}/ownership-summary")
async def ownership_summary(user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    await _user(db, ctx, user_id)
    return await _ownership(db, user_id)


class TransferIn(BaseModel):
    from_user_id: str
    to_user_id: str


@router.post("/iam-users/transfer-ownership")
async def transfer_ownership(body: TransferIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    src, dst = await _user(db, ctx, body.from_user_id), await _user(db, ctx, body.to_user_id)
    if src.id == dst.id:
        raise bad_request("SAME_USER", "Pick a different person to receive ownership.")
    if dst.status == "deactivated":
        raise bad_request("TARGET_DEACTIVATED", "The receiving user is deactivated.")
    details = []
    moved = (await db.execute(update(Matter).where(Matter.created_by == src.id).values(created_by=dst.id))).rowcount
    details.append({"action": "MATTER_CREATOR_REASSIGNED", "count": moved})
    moved = (await db.execute(update(Collection).where(Collection.created_by == src.id).values(created_by=dst.id))).rowcount
    details.append({"action": "COLLECTION_CREATOR_REASSIGNED", "count": moved})
    for entry in (await db.scalars(select(MatterMember).where(MatterMember.principal_type == "user",
                                                              MatterMember.principal_id == src.id))).all():
        if entry.role == P.BLOCKED:
            continue
        existing = await db.scalar(select(MatterMember).where(MatterMember.matter_id == entry.matter_id,
                                                        MatterMember.principal_type == "user",
                                                        MatterMember.principal_id == dst.id))
        if existing is None:
            entry.principal_id = dst.id
            details.append({"action": "ROLE_MOVED", "matter_id": entry.matter_id, "role": entry.role})
        elif existing.role == P.BLOCKED:
            details.append({"action": "SKIPPED_WALLED", "matter_id": entry.matter_id})
        else:
            if P.MATTER_RANK[entry.role] > P.MATTER_RANK[existing.role]:
                existing.role = entry.role
                details.append({"action": "ROLE_MERGED_PROMOTED", "matter_id": entry.matter_id, "role": entry.role})
            await db.delete(entry)
    db.add(OwnershipTransfer(firm_id=ctx.who.firm_id, from_user_id=src.id, to_user_id=dst.id, details=details,
                             performed_by=ctx.who.id))
    audit.record_ctx(db, ctx, "user.ownership_transferred", "iam_user", src.id, to=dst.id)
    await db.commit()
    return {"details": details, "remaining": await _ownership(db, src.id)}


@router.get("/ownership-transfers")
async def list_transfers(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    rows = await db.scalars(select(OwnershipTransfer).where(OwnershipTransfer.firm_id == ctx.who.firm_id)
                      .order_by(OwnershipTransfer.performed_at.desc()).limit(100))
    return {"items": [{"id": r.id, "from_user_id": r.from_user_id, "to_user_id": r.to_user_id, "details": r.details,
                       "performed_by": r.performed_by, "performed_at": iso(r.performed_at)} for r in rows]}


@router.delete("/iam-users/{user_id}")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    user = await _user(db, ctx, user_id)
    if not (await _ownership(db, user.id))["can_delete"]:
        raise conflict("OWNS_ASSETS", "Transfer this user's matters and collections first.")
    await sessions.revoke_all(db, "iam", user.id)
    await db.execute(delete(MatterMember).where(MatterMember.principal_type == "user", MatterMember.principal_id == user.id))
    await get_provider("iam").delete_user(db, user.email, subject=user.provider_subject)
    audit.record_ctx(db, ctx, "user.deleted", "iam_user", user.id, email=user.email)
    await db.delete(user)
    await db.commit()
    return {"status": "success"}


# Admins ---------------------------------------------------------------------------------
class InviteAdminIn(BaseModel):
    email: Email
    name: str = Field(min_length=1, max_length=200)


@router.get("/admins")
async def list_admins(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    return {"items": [admin_out(a) for a in await db.scalars(select(Admin).where(Admin.firm_id == ctx.who.firm_id)
                                                        .order_by(Admin.name))]}


@router.post("/admins", status_code=201)
async def invite_admin(body: InviteAdminIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    email = body.email.lower()
    if await db.scalar(select(Admin).where(Admin.email == email)):
        raise conflict("EMAIL_EXISTS", "An admin with this email already exists.")
    provider = get_provider("admin")
    subject, temp = await provider.create_user(db, email, body.name.strip(),
                                                     organization_id=(await db.get(Firm, ctx.who.firm_id)).workos_organization_id)
    admin = Admin(firm_id=ctx.who.firm_id, email=email, name=body.name.strip(), provider_subject=subject,
                  status="invited", created_by=ctx.who.id)
    db.add(admin)
    await db.flush()
    audit.record_ctx(db, ctx, "admin.invited", "admin", admin.id, email=email)
    await db.commit()
    out = {"admin": admin_out(admin)}
    if get_settings().dev_auth and temp:
        out["dev_temporary_password"] = temp
    return out


@router.post("/admins/{admin_id}/deactivate")
async def deactivate_admin(admin_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    admin = await db.get(Admin, admin_id)
    if not admin or admin.firm_id != ctx.who.firm_id:
        raise not_found("Admin")
    if admin.id == ctx.who.id:
        raise bad_request("SELF", "You can't deactivate your own admin account.")
    active = await db.scalar(select(func.count()).select_from(Admin).where(
        Admin.firm_id == ctx.who.firm_id, Admin.status != "deactivated"))
    if active <= 1:
        raise conflict("LAST_ADMIN", "A firm must keep at least one admin.")
    admin.status = "deactivated"
    await sessions.revoke_all(db, "admin", admin.id)
    await get_provider("admin").set_enabled(db, admin.email, False)
    audit.record_ctx(db, ctx, "admin.deactivated", "admin", admin.id)
    await db.commit()
    return {"admin": admin_out(admin)}


@router.post("/admins/{admin_id}/reactivate")
async def reactivate_admin(admin_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    admin = await db.get(Admin, admin_id)
    if not admin or admin.firm_id != ctx.who.firm_id:
        raise not_found("Admin")
    admin.status = "active" if admin.last_login_at else "invited"
    await get_provider("admin").set_enabled(db, admin.email, True)
    audit.record_ctx(db, ctx, "admin.reactivated", "admin", admin.id)
    await db.commit()
    return {"admin": admin_out(admin)}


# Teams ----------------------------------------------------------------------------------
class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class TeamMemberIn(BaseModel):
    user_id: str


async def _team(db: AsyncSession, ctx: Ctx, team_id: str) -> Team:
    team = await db.get(Team, team_id)
    if not team or team.firm_id != ctx.who.firm_id:
        raise not_found("Team")
    return team


async def _team_out(db: AsyncSession, team: Team) -> dict:
    members = await db.scalars(select(IamUser).join(TeamMember, TeamMember.user_id == IamUser.id)
                         .where(TeamMember.team_id == team.id).order_by(IamUser.name))
    return {"id": team.id, "name": team.name, "description": team.description,
            "members": [{"id": u.id, "name": u.name, "email": u.email, "firm_role": u.firm_role} for u in members]}


@router.get("/teams")
async def list_teams(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    return {"items": [await _team_out(db, t) for t in await db.scalars(select(Team).where(Team.firm_id == ctx.who.firm_id)
                                                            .order_by(Team.name))]}


@router.post("/teams", status_code=201)
async def create_team(body: TeamIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    if await db.scalar(select(Team).where(Team.firm_id == ctx.who.firm_id, Team.name == body.name.strip())):
        raise conflict("TEAM_EXISTS", "A team with this name already exists.")
    team = Team(firm_id=ctx.who.firm_id, name=body.name.strip(), description=body.description)
    db.add(team)
    await db.flush()
    audit.record_ctx(db, ctx, "team.created", "team", team.id, name=team.name)
    await db.commit()
    return {"team": await _team_out(db, team)}


@router.patch("/teams/{team_id}")
async def update_team(team_id: str, body: TeamIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    team = await _team(db, ctx, team_id)
    team.name, team.description = body.name.strip(), body.description
    audit.record_ctx(db, ctx, "team.updated", "team", team.id)
    await db.commit()
    return {"team": await _team_out(db, team)}


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    team = await _team(db, ctx, team_id)
    lead_entries = (await db.scalars(select(MatterMember).where(MatterMember.principal_type == "team",
                                                                MatterMember.principal_id == team.id))).all()
    await db.execute(delete(MatterMember).where(MatterMember.principal_type == "team", MatterMember.principal_id == team.id))
    audit.record_ctx(db, ctx, "team.deleted", "team", team.id, name=team.name, matter_entries=len(lead_entries))
    await db.delete(team)
    await db.commit()
    return {"status": "success"}


@router.post("/teams/{team_id}/members")
async def add_team_member(team_id: str, body: TeamMemberIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    team = await _team(db, ctx, team_id)
    await _user(db, ctx, body.user_id)
    if not await db.get(TeamMember, (team.id, body.user_id)):
        db.add(TeamMember(team_id=team.id, user_id=body.user_id))
        audit.record_ctx(db, ctx, "team.member_added", "team", team.id, user_id=body.user_id)
    await db.commit()
    return {"team": await _team_out(db, team)}


@router.delete("/teams/{team_id}/members/{user_id}")
async def remove_team_member(team_id: str, user_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    team = await _team(db, ctx, team_id)
    row = await db.get(TeamMember, (team.id, user_id))
    if row:
        await db.delete(row)
        audit.record_ctx(db, ctx, "team.member_removed", "team", team.id, user_id=user_id)
    await db.commit()
    return {"team": await _team_out(db, team)}


# Audit, sessions, settings ----------------------------------------------------------------
def _audit_out(db: AsyncSession, e: AuditEvent) -> dict:
    return {"id": e.id, "action": e.action, "actor_type": e.actor_type, "actor_id": e.actor_id,
            "target_type": e.target_type, "target_id": e.target_id, "ip": e.ip, "data": e.data,
            "created_at": iso(e.created_at)}


@router.get("/audit")
async def list_audit(action: str = "", actor_id: str = "", before_id: int | None = None, limit: int = 100,
                     db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    stmt = select(AuditEvent).where(AuditEvent.firm_id == ctx.who.firm_id)
    if action:
        stmt = stmt.where(AuditEvent.action.like(f"{action}%"))
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if before_id:
        stmt = stmt.where(AuditEvent.id < before_id)
    rows = (await db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(min(limit, 500)))).all()
    names = {a.id: a.name for a in await db.scalars(select(Admin).where(Admin.firm_id == ctx.who.firm_id))}
    names.update({u.id: u.name for u in await db.scalars(select(IamUser).where(IamUser.firm_id == ctx.who.firm_id))})
    return {"items": [dict(_audit_out(db, e), actor_name=names.get(e.actor_id)) for e in rows]}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    rows = (await db.scalars(select(AuthSession).where(AuthSession.firm_id == ctx.who.firm_id,
                                                       AuthSession.revoked_at.is_(None))
                             .order_by(AuthSession.last_seen_at.desc()).limit(300))).all()
    names = {a.id: a.name for a in await db.scalars(select(Admin).where(Admin.firm_id == ctx.who.firm_id))}
    names.update({u.id: u.name for u in await db.scalars(select(IamUser).where(IamUser.firm_id == ctx.who.firm_id))})
    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        expires = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
        if expires <= now:
            continue
        items.append({"id": r.id, "console": r.console, "principal_id": r.principal_id,
                      "principal_name": names.get(r.principal_id), "ip": r.ip, "user_agent": r.user_agent,
                      "created_at": iso(r.created_at), "last_seen_at": iso(r.last_seen_at),
                      "current": r.id == ctx.session.id})
    return {"items": items}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    row = await db.get(AuthSession, session_id)
    if not row or row.firm_id != ctx.who.firm_id:
        raise not_found("Session")
    sessions.revoke(db, row)
    audit.record_ctx(db, ctx, "session.revoked", "session", row.id, principal_id=row.principal_id)
    await db.commit()
    return {"status": "success"}


class FirmSettingsIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    require_mfa: bool | None = None
    allowed_email_domains: list[str] | None = None


def _firm_out(firm: Firm) -> dict:
    return {"id": firm.id, "name": firm.name, "slug": firm.slug, "require_mfa": firm.require_mfa,
            "allowed_email_domains": firm.allowed_email_domains or []}


@router.get("/firm-settings")
async def get_firm_settings(db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    return {"firm": _firm_out(await db.get(Firm, ctx.who.firm_id))}


@router.patch("/firm-settings")
async def update_firm_settings(body: FirmSettingsIn, db: AsyncSession = Depends(get_db), ctx: Ctx = ADMIN):
    firm = await db.get(Firm, ctx.who.firm_id)
    changes = body.model_dump(exclude_none=True)
    if "allowed_email_domains" in changes:
        changes["allowed_email_domains"] = sorted({d.strip().lower().lstrip("@") for d in changes["allowed_email_domains"]
                                                   if d.strip()})
    for key, value in changes.items():
        setattr(firm, key, value)
    audit.record_ctx(db, ctx, "firm.settings_changed", "firm", firm.id, **changes)
    await db.commit()
    return {"firm": _firm_out(firm)}
