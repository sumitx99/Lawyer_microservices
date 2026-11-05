"""Matters and their teams (ACL). Every route goes through require(perm, matter=...)."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import bad_request, conflict, not_found
from pymicroservices.app.firm_models import (Collection, IamUser, Matter, MatterCollection, MatterMember, Team, TeamMember)
from pymicroservices.app.security import access
from pymicroservices.app.security import permissions as P
from pymicroservices.app.security.guard import Ctx, require
from pymicroservices.app.services.firm import audit
from pymicroservices.app.services.collections.collection_files import collection_counts
from pymicroservices.app.services.collections.collection_search import SearchIn, search
from pymicroservices.app.services.firm.serialize import iso, matter_out

router = APIRouter(prefix="/api/matters", tags=["matters"])


def _now():
    return datetime.now(timezone.utc)


class MatterIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    client_name: str = Field(min_length=1, max_length=200)
    practice_area: str = Field(default="Personal injury", max_length=80)
    number: str | None = Field(default=None, max_length=40)
    lead_user_id: str | None = None  # required when an admin creates a matter


class MatterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    client_name: str | None = Field(default=None, min_length=1, max_length=200)
    practice_area: str | None = Field(default=None, max_length=80)


def _next_number(db: Session, firm_id: str) -> str:
    count = db.scalar(select(func.count()).select_from(Matter).where(Matter.firm_id == firm_id)) or 0
    while True:
        count += 1
        number = f"M-{count:04d}"
        if not db.scalar(select(Matter.id).where(Matter.firm_id == firm_id, Matter.number == number)):
            return number


def _detail(db: Session, ctx: Ctx, matter: Matter, perms: set[str], role: str | None) -> dict:
    linked = db.scalar(select(func.count()).select_from(MatterCollection).where(MatterCollection.matter_id == matter.id))
    return matter_out(matter, my_role=role, flags=access.matter_flags(perms), collections=linked,
                      grantable_roles=list(access.grantable_roles(ctx.who, perms, role)))


@router.get("")
def list_matters(q: str = "", status: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.list"))):
    stmt = access.visible_matters(ctx.who)
    if q:
        stmt = stmt.where(or_(Matter.name.ilike(f"%{q}%"), Matter.client_name.ilike(f"%{q}%"), Matter.number.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(Matter.status == status)
    items = []
    for m in db.scalars(stmt.order_by(Matter.opened_at.desc()).limit(500)):
        perms, role, _ = access.matter_perms(db, ctx.who, m.id)
        items.append(matter_out(m, my_role=role, flags=access.matter_flags(perms)))
    return {"items": items}


@router.post("", status_code=201)
def create_matter(body: MatterIn, db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.create"))):
    number = (body.number or "").strip() or _next_number(db, ctx.who.firm_id)
    if db.scalar(select(Matter.id).where(Matter.firm_id == ctx.who.firm_id, Matter.number == number)):
        raise conflict("NUMBER_EXISTS", f"Matter number {number} is already used.")
    if ctx.who.is_admin:
        if not body.lead_user_id:
            raise bad_request("LEAD_REQUIRED", "Choose a Lead for this matter.")
        lead = db.get(IamUser, body.lead_user_id)
        if not lead or lead.firm_id != ctx.who.firm_id or lead.status == "deactivated":
            raise bad_request("UNKNOWN_USER", "That Lead isn't an active user in your firm.")
        lead_id = lead.id
    else:
        lead_id = ctx.who.id
    matter = Matter(firm_id=ctx.who.firm_id, number=number, name=body.name.strip(), client_name=body.client_name.strip(),
                    practice_area=body.practice_area.strip() or "Personal injury",
                    created_by=None if ctx.who.is_admin else ctx.who.id,
                    created_by_admin=ctx.who.id if ctx.who.is_admin else None)
    db.add(matter)
    db.flush()
    db.add(MatterMember(matter_id=matter.id, principal_type="user", principal_id=lead_id, role="LEAD",
                        assigned_by=ctx.who.id))
    audit.record_ctx(db, ctx, "matter.created", "matter", matter.id, number=number, lead=lead_id)
    db.commit()
    perms, role, _ = access.matter_perms(db, ctx.who, matter.id)
    return {"matter": _detail(db, ctx, matter, perms, role)}


@router.get("/{matter_id}")
def get_matter(matter_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.read", matter="matter_id"))):
    return {"matter": _detail(db, ctx, ctx.matter, ctx.perms, ctx.matter_role)}


@router.patch("/{matter_id}")
def update_matter(matter_id: str, body: MatterPatch, db: Session = Depends(get_db),
                  ctx: Ctx = Depends(require("matters.update", matter="matter_id"))):
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(ctx.matter, key, value.strip())
    audit.record_ctx(db, ctx, "matter.updated", "matter", matter_id, **changes)
    db.commit()
    return {"matter": _detail(db, ctx, ctx.matter, ctx.perms, ctx.matter_role)}


@router.post("/{matter_id}/close")
def close_matter(matter_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.close", matter="matter_id"))):
    ctx.matter.status, ctx.matter.closed_at = "closed", _now()
    audit.record_ctx(db, ctx, "matter.closed", "matter", matter_id)
    db.commit()
    return {"matter": _detail(db, ctx, ctx.matter, ctx.perms, ctx.matter_role)}


@router.post("/{matter_id}/reopen")
def reopen_matter(matter_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.close", matter="matter_id"))):
    ctx.matter.status, ctx.matter.closed_at = "open", None
    audit.record_ctx(db, ctx, "matter.reopened", "matter", matter_id)
    db.commit()
    return {"matter": _detail(db, ctx, ctx.matter, ctx.perms, ctx.matter_role)}


@router.delete("/{matter_id}")
def delete_matter(matter_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require("matters.delete", matter="matter_id"))):
    ctx.matter.deleted_at = _now()  # soft delete: firms have retention rules
    audit.record_ctx(db, ctx, "matter.deleted", "matter", matter_id)
    db.commit()
    return {"status": "success"}


# Members ---------------------------------------------------------------------------------
class MemberChange(BaseModel):
    principal_type: Literal["user", "team"]
    principal_id: str
    action: Literal["assign", "remove"]
    role: Literal["LEAD", "EDITOR", "VIEWER", "BLOCKED"] | None = None
    expected_role: str | None = None  # current role the caller saw; mismatch -> MEMBERS_CHANGED
    wall_reason: str | None = Field(default=None, max_length=500)


class MembersIn(BaseModel):
    changes: list[MemberChange] = Field(min_length=1, max_length=50)
    new_lead_user_id: str | None = None


def _member_rows(db: Session, matter_id: str) -> list[MatterMember]:
    return db.scalars(select(MatterMember).where(MatterMember.matter_id == matter_id)).all()


def _members_out(db: Session, ctx: Ctx, matter_id: str) -> list[dict]:
    rows = _member_rows(db, matter_id)
    can_see_walls = "members.manage" in (ctx.perms or set())
    users = {u.id: u for u in db.scalars(select(IamUser).where(IamUser.id.in_(
        [r.principal_id for r in rows if r.principal_type == "user"])))}
    teams = {t.id: t for t in db.scalars(select(Team).where(Team.id.in_(
        [r.principal_id for r in rows if r.principal_type == "team"])))}
    out = []
    for r in rows:
        if r.role == P.BLOCKED and not can_see_walls:
            continue
        if r.principal_type == "user":
            u = users.get(r.principal_id)
            label, sub = (u.name, u.email) if u else ("Removed user", "")
            extra = {"firm_role": u.firm_role if u else None}
        else:
            t = teams.get(r.principal_id)
            count = db.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == r.principal_id))
            label, sub = (t.name, f"{count} people") if t else ("Removed team", "")
            extra = {}
        out.append({"id": r.id, "principal_type": r.principal_type, "principal_id": r.principal_id, "name": label,
                    "detail": sub, "role": r.role, "wall_reason": r.wall_reason if can_see_walls else None,
                    "assigned_at": iso(r.assigned_at), **extra})
    order = {"LEAD": 0, "EDITOR": 1, "VIEWER": 2, P.BLOCKED: 3}
    return sorted(out, key=lambda m: (order.get(m["role"], 9), m["name"].lower()))


def _validate(db: Session, ctx: Ctx, matter: Matter, body: MembersIn) -> tuple[list[str], dict]:
    """SLR's _member_changes_error, adapted. Returns (errors, planned {key: role|None})."""
    errors: list[str] = []
    grantable = set(access.grantable_roles(ctx.who, ctx.perms or set(), ctx.matter_role))
    current = {(r.principal_type, r.principal_id): r.role for r in _member_rows(db, matter.id)}
    planned = dict(current)
    seen = set()
    for ch in body.changes:
        key = (ch.principal_type, ch.principal_id)
        if key in seen:
            errors.append("Only one change per person or team in each request.")
            continue
        seen.add(key)
        if ch.expected_role is not None and current.get(key) != (ch.expected_role or None):
            raise conflict("MEMBERS_CHANGED", "Someone else changed this matter's team. Reload and try again.")
        if ch.principal_type == "user":
            user = db.get(IamUser, ch.principal_id)
            if not user or user.firm_id != ctx.who.firm_id:
                errors.append("That person isn't in your firm.")
                continue
        else:
            team = db.get(Team, ch.principal_id)
            if not team or team.firm_id != ctx.who.firm_id:
                errors.append("That team doesn't exist.")
                continue
        is_self = ch.principal_type == "user" and ch.principal_id == ctx.who.id
        if ch.action == "assign":
            if not ch.role:
                errors.append("Pick a role.")
                continue
            if ch.role not in grantable:
                errors.append(f"You can't give the {ch.role.title()} role.")
                continue
            if current.get(key) and current[key] not in grantable:
                errors.append("You can't change this person's current role.")
                continue
            if ch.role == P.BLOCKED:
                if ch.principal_type != "user":
                    errors.append("Ethical walls apply to people, not teams.")
                    continue
                if is_self:
                    errors.append("You can't wall yourself off a matter.")
                    continue
                if not (ch.wall_reason or "").strip():
                    errors.append("An ethical wall needs a reason.")
                    continue
            planned[key] = ch.role
        else:
            if key not in current:
                errors.append("That person or team isn't on this matter.")
                continue
            if is_self and not (ctx.who.is_admin or P.is_ceiling(ctx.who.firm_role) or ctx.matter_role == "LEAD"):
                errors.append("You can't remove yourself from this matter.")
                continue
            if current[key] not in grantable:
                errors.append("You can't remove someone with this role.")
                continue
            planned[key] = None
    return errors, planned


def _apply(db: Session, ctx: Ctx, matter: Matter, body: MembersIn) -> dict:
    errors, planned = _validate(db, ctx, matter, body)
    if errors:
        raise bad_request("INVALID_MEMBER_CHANGES", errors[0], errors=errors)
    leads = [k for k, role in planned.items() if role == "LEAD" and k[0] == "user"]
    if not leads:
        if not body.new_lead_user_id:
            raise conflict("NEEDS_HANDOVER", "A matter needs at least one Lead. Choose who takes over.", needs_handover=True)
        new_lead = db.get(IamUser, body.new_lead_user_id)
        if not new_lead or new_lead.firm_id != ctx.who.firm_id or new_lead.status == "deactivated" \
                or planned.get(("user", new_lead.id)) == P.BLOCKED:
            raise bad_request("INVALID_HANDOVER", "That person can't become Lead.")
        planned[("user", new_lead.id)] = "LEAD"
    rows = {(r.principal_type, r.principal_id): r for r in _member_rows(db, matter.id)}
    reasons = {(c.principal_type, c.principal_id): c.wall_reason for c in body.changes}
    for key, role in planned.items():
        row = rows.get(key)
        before = row.role if row else None
        if role == before:
            continue
        if role is None:
            db.delete(row)
            action = "wall.removed" if before == P.BLOCKED else "acl.revoked"
        elif row is None:
            db.add(MatterMember(matter_id=matter.id, principal_type=key[0], principal_id=key[1], role=role,
                                wall_reason=(reasons.get(key) or "").strip() or None, assigned_by=ctx.who.id))
            action = "wall.added" if role == P.BLOCKED else "acl.granted"
        else:
            row.role, row.assigned_by, row.assigned_at = role, ctx.who.id, _now()
            row.wall_reason = (reasons.get(key) or "").strip() or None if role == P.BLOCKED else None
            action = "wall.added" if role == P.BLOCKED else "acl.changed"
        audit.record_ctx(db, ctx, action, "matter", matter.id, principal_type=key[0], principal_id=key[1],
                         old=before, new=role)
    db.commit()
    perms, role, _ = access.matter_perms(db, ctx.who, matter.id)
    ctx.perms, ctx.matter_role = perms, role
    return {"members": _members_out(db, ctx, matter.id), "matter": _detail(db, ctx, matter, perms, role)}


@router.get("/{matter_id}/members")
def list_members(matter_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require("members.read", matter="matter_id"))):
    return {"members": _members_out(db, ctx, matter_id),
            "grantable_roles": list(access.grantable_roles(ctx.who, ctx.perms, ctx.matter_role))}


@router.post("/{matter_id}/members/validate")
def validate_members(matter_id: str, body: MembersIn, db: Session = Depends(get_db),
                     ctx: Ctx = Depends(require("members.validate", matter="matter_id"))):
    errors, planned = _validate(db, ctx, ctx.matter, body)
    leads = [k for k, role in planned.items() if role == "LEAD" and k[0] == "user"]
    return {"ok": not errors and bool(leads or body.new_lead_user_id), "errors": errors,
            "needs_handover": not leads and not body.new_lead_user_id}


@router.post("/{matter_id}/members")
def change_members(matter_id: str, body: MembersIn, db: Session = Depends(get_db),
                   ctx: Ctx = Depends(require("members.grant", matter="matter_id"))):
    return _apply(db, ctx, ctx.matter, body)


# Collections linked to a matter ----------------------------------------------------------------
class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


def _linked_out(db: Session, ctx: Ctx, matter_id: str) -> list[dict]:
    cols = db.scalars(select(Collection).join(MatterCollection, MatterCollection.collection_id == Collection.id)
                      .where(MatterCollection.matter_id == matter_id, Collection.deleted_at.is_(None))
                      .order_by(Collection.name)).all()
    counts = collection_counts(db, [c.id for c in cols])
    out = []
    for c in cols:
        perms, visible = access.collection_perms(db, ctx.who, c)
        if visible:
            out.append({"id": c.id, "name": c.name, "description": c.description, "documents": counts.get(c.id, 0),
                        "created_at": iso(c.created_at), "flags": access.collection_flags(perms)})
    return out


@router.get("/{matter_id}/collections")
def matter_collections(matter_id: str, db: Session = Depends(get_db),
                       ctx: Ctx = Depends(require("collections.read", matter="matter_id"))):
    return {"items": _linked_out(db, ctx, matter_id)}


@router.post("/{matter_id}/collections", status_code=201)
def create_matter_collection(matter_id: str, body: CollectionIn, db: Session = Depends(get_db),
                             ctx: Ctx = Depends(require("collections.create", matter="matter_id"))):
    col = Collection(firm_id=ctx.who.firm_id, name=body.name.strip(), description=body.description.strip(),
                     created_by=None if ctx.who.is_admin else ctx.who.id,
                     created_by_admin=ctx.who.id if ctx.who.is_admin else None)
    db.add(col)
    db.flush()
    db.add(MatterCollection(matter_id=matter_id, collection_id=col.id, linked_by=ctx.who.id))
    audit.record_ctx(db, ctx, "collection.created", "collection", col.id, matter_id=matter_id, name=col.name)
    db.commit()
    return {"items": _linked_out(db, ctx, matter_id)}


@router.post("/{matter_id}/collections/{collection_id}/link")
def link_collection(matter_id: str, collection_id: str, db: Session = Depends(get_db),
                    ctx: Ctx = Depends(require("collections.link", matter="matter_id"))):
    access.authorize_collection(db, ctx.who, "collections.read", collection_id)  # must be able to read it too
    if not db.get(MatterCollection, (matter_id, collection_id)):
        db.add(MatterCollection(matter_id=matter_id, collection_id=collection_id, linked_by=ctx.who.id))
        audit.record_ctx(db, ctx, "collection.linked", "collection", collection_id, matter_id=matter_id)
        db.commit()
    return {"items": _linked_out(db, ctx, matter_id)}


@router.delete("/{matter_id}/collections/{collection_id}")
def unlink_collection(matter_id: str, collection_id: str, db: Session = Depends(get_db),
                      ctx: Ctx = Depends(require("collections.unlink", matter="matter_id"))):
    row = db.get(MatterCollection, (matter_id, collection_id))
    if not row:
        raise not_found("Linked collection")
    db.delete(row)
    audit.record_ctx(db, ctx, "collection.unlinked", "collection", collection_id, matter_id=matter_id)
    db.commit()
    return {"items": _linked_out(db, ctx, matter_id)}


@router.post("/{matter_id}/search")
async def search_matter(matter_id: str, body: SearchIn, request: Request, db: Session = Depends(get_db),
                        ctx: Ctx = Depends(require("documents.read", matter="matter_id"))):
    """Search every collection linked to the matter that the caller can read (walls apply per collection)."""
    cols = db.scalars(select(Collection).join(MatterCollection, MatterCollection.collection_id == Collection.id)
                      .where(MatterCollection.matter_id == matter_id, Collection.deleted_at.is_(None))).all()
    readable = [c.id for c in cols if "documents.read" in access.collection_perms(db, ctx.who, c)[0]]
    return await search(request.app.state, readable, matter_id, body)
