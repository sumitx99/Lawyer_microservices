"""The RBAC + ACL engine. Everything that decides access lives here.

Order of a matter check:
  admin (Admin console)          -> allowed, walls don't apply, document access is audited
  walled on the matter           -> 404 (existence isn't revealed)
  ceiling firm role with perm    -> allowed
  matter role (direct or team, highest wins) with perm -> allowed
  ATTORNEY_ONLY perms also need a licensed firm role
Lists use the same rules as SQL (visible_matters / visible_collections), so an item can
never appear in a list and then refuse to open."""
from dataclasses import dataclass

from sqlalchemy import and_, exists, literal, or_, select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.errors import forbidden, not_found
from pymicroservices.app.firm_models import (Collection, CollectionGrant, Matter, MatterCollection, MatterMember, TeamMember)
from pymicroservices.app.security import permissions as P

MATTER_SCOPED = set(P.MATTER_READ + P.MATTER_WRITE + P.MATTER_OWNER + P.ATTORNEY_ONLY)
READ_PERMS = set(P.MATTER_READ)
WRITE_PERMS = set(P.MATTER_WRITE)


@dataclass
class Principal:
    console: str  # admin | iam
    id: str
    firm_id: str
    email: str
    name: str
    firm_role: str | None  # None for admins

    @property
    def is_admin(self) -> bool:
        return self.console == "admin"


# Matter roles ---------------------------------------------------------------------
def matter_role(db: Session, who: Principal, matter_id: str) -> str | None:
    """Best role for an IAM user on a matter: BLOCKED (wall) beats everything,
    otherwise the highest of direct and team roles."""
    team_ids = select(TeamMember.team_id).where(TeamMember.user_id == who.id)
    rows = db.scalars(select(MatterMember.role).where(
        MatterMember.matter_id == matter_id,
        or_(and_(MatterMember.principal_type == "user", MatterMember.principal_id == who.id),
            and_(MatterMember.principal_type == "team", MatterMember.principal_id.in_(team_ids))))).all()
    if P.BLOCKED in rows:
        return P.BLOCKED
    ranked = [r for r in rows if r in P.MATTER_RANK]
    return max(ranked, key=P.MATTER_RANK.get) if ranked else None


def matter_perms(db: Session, who: Principal, matter_id: str) -> tuple[set[str], str | None, bool]:
    """(permissions on this matter, matter role, visible?)"""
    if who.is_admin:
        return set(P.ALL_PERMISSIONS), "ADMIN", True
    role = matter_role(db, who, matter_id)
    if role == P.BLOCKED:
        return set(), role, False
    perms: set[str] = set()
    ceiling = P.is_ceiling(who.firm_role)
    if ceiling:
        perms |= P.firm_permissions(who.firm_role) & MATTER_SCOPED
    perms |= P.matter_permissions(role)
    if not P.is_licensed(who.firm_role):
        perms -= set(P.ATTORNEY_ONLY)
    return perms, role, (role is not None or ceiling)


def load_matter(db: Session, who: Principal, matter_id: str) -> Matter:
    matter = db.get(Matter, matter_id)
    if not matter or matter.firm_id != who.firm_id or matter.deleted_at is not None:
        raise not_found("Matter")
    return matter


def authorize_matter(db: Session, who: Principal, perm: str, matter_id: str) -> tuple[Matter, set[str], str | None]:
    matter = load_matter(db, who, matter_id)
    perms, role, visible = matter_perms(db, who, matter_id)
    if not visible:
        raise not_found("Matter")
    if perm not in perms:
        raise forbidden()
    if matter.status == "closed" and perm in WRITE_PERMS and perm != "members.validate":
        raise forbidden("This matter is closed.")
    return matter, perms, role


def authorize_firm(who: Principal, perm: str) -> None:
    if who.is_admin:
        return
    if perm not in P.firm_permissions(who.firm_role):
        raise forbidden()


def grantable_roles(who: Principal, perms: set[str], role: str | None) -> tuple[str, ...]:
    if who.is_admin or (P.is_ceiling(who.firm_role) and "members.manage" in perms):
        return P.ALL_GRANTS
    return P.GRANTS_BY_MATTER_ROLE.get(role or "", ())


# Collections ------------------------------------------------------------------------
def linked_matter_ids(db: Session, collection_id: str) -> list[str]:
    return list(db.scalars(select(MatterCollection.matter_id).where(MatterCollection.collection_id == collection_id)))


def load_collection(db: Session, who: Principal, collection_id: str) -> Collection:
    col = db.get(Collection, collection_id)
    if not col or col.firm_id != who.firm_id or col.deleted_at is not None:
        raise not_found("Collection")
    return col


def collection_perms(db: Session, who: Principal, col: Collection) -> tuple[set[str], bool]:
    """(permissions on this collection, visible?). Walled on ANY linked matter -> hidden."""
    if who.is_admin:
        return set(P.ALL_PERMISSIONS), True
    perms: set[str] = set()
    matters = linked_matter_ids(db, col.id)
    for mid in matters:
        mperms, role, visible = matter_perms(db, who, mid)
        if role == P.BLOCKED:
            return set(), False
        if visible:
            perms |= mperms
    grant = db.get(CollectionGrant, (col.id, who.id))
    if grant:
        perms |= READ_PERMS | ({"documents.upload", "collections.update"} if grant.level == "write" else set())
    if not matters:
        if col.created_by == who.id:
            perms |= READ_PERMS | WRITE_PERMS | {"collections.delete", "documents.delete", "collections.share"}
        if P.is_ceiling(who.firm_role):
            perms |= P.firm_permissions(who.firm_role) & MATTER_SCOPED
    perms -= {"collections.unlink"} if not matters else set()
    return perms, bool(perms)


def authorize_collection(db: Session, who: Principal, perm: str, collection_id: str) -> tuple[Collection, set[str]]:
    col = load_collection(db, who, collection_id)
    perms, visible = collection_perms(db, who, col)
    if not visible:
        raise not_found("Collection")
    if perm not in perms:
        raise forbidden()
    return col, perms


# List scopes as SQL -------------------------------------------------------------------
def _user_entry(who: Principal, roles: tuple[str, ...]):
    team_ids = select(TeamMember.team_id).where(TeamMember.user_id == who.id)
    return exists().where(
        MatterMember.matter_id == Matter.id, MatterMember.role.in_(roles),
        or_(and_(MatterMember.principal_type == "user", MatterMember.principal_id == who.id),
            and_(MatterMember.principal_type == "team", MatterMember.principal_id.in_(team_ids))))


def _walled():
    return lambda who: exists().where(MatterMember.matter_id == Matter.id, MatterMember.role == P.BLOCKED,
                                      MatterMember.principal_type == "user", MatterMember.principal_id == who.id)


def visible_matters(who: Principal):
    """SELECT of matters this principal may see. Use as .where(Matter.id.in_(...)) or directly."""
    base = select(Matter).where(Matter.firm_id == who.firm_id, Matter.deleted_at.is_(None))
    if who.is_admin:
        return base
    walled = _walled()(who)
    if P.is_ceiling(who.firm_role):
        return base.where(~walled)
    return base.where(~walled, _user_entry(who, ("LEAD", "EDITOR", "VIEWER")))


def visible_matter_ids(who: Principal):
    return visible_matters(who).with_only_columns(Matter.id)


def visible_collections(who: Principal):
    base = select(Collection).where(Collection.firm_id == who.firm_id, Collection.deleted_at.is_(None))
    if who.is_admin:
        return base
    linked = exists().where(MatterCollection.collection_id == Collection.id)
    walled_link = exists().where(
        MatterCollection.collection_id == Collection.id,
        MatterCollection.matter_id.in_(select(MatterMember.matter_id).where(
            MatterMember.role == P.BLOCKED, MatterMember.principal_type == "user",
            MatterMember.principal_id == who.id)))
    via_matter = exists().where(MatterCollection.collection_id == Collection.id,
                                MatterCollection.matter_id.in_(visible_matter_ids(who)))
    via_grant = exists().where(CollectionGrant.collection_id == Collection.id, CollectionGrant.user_id == who.id)
    own_unlinked = and_(Collection.created_by == who.id, ~linked)
    ceiling_unlinked = and_(literal(P.is_ceiling(who.firm_role)), ~linked)
    return base.where(~walled_link, or_(via_matter, via_grant, own_unlinked, ceiling_unlinked))


# UI flags ----------------------------------------------------------------------------------
def matter_flags(perms: set[str]) -> dict[str, bool]:
    return {
        "can_edit": "matters.update" in perms,
        "can_upload": "documents.upload" in perms,
        "can_link_collections": "collections.link" in perms,
        "can_manage_members": "members.manage" in perms,
        "can_grant": "members.grant" in perms,
        "can_close": "matters.close" in perms,
        "can_delete": "matters.delete" in perms,
        "can_delete_documents": "documents.delete" in perms,
        "can_approve_drafts": "drafts.approve" in perms,
    }


def collection_flags(perms: set[str]) -> dict[str, bool]:
    return {
        "can_upload": "documents.upload" in perms,
        "can_edit": "collections.update" in perms or "documents.upload" in perms,
        "can_delete": "collections.delete" in perms,
        "can_delete_documents": "documents.delete" in perms,
        "can_unlink": "collections.unlink" in perms,
    }
