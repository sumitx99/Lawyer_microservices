"""The role catalogue: single source of truth, synced to `permission_sets` at startup.

Permissions are action names (not URL paths, which broke on path parameters in SLR).
Every route declares the permission it needs through `require(...)` in guard.py, and the
app refuses to boot if a route has no guard.

Layers
  firm role   (iam_users.firm_role)      -> what a user may do anywhere in the firm
  matter role (matter_members.role)      -> what they may do on one matter
  ATTORNEY_ONLY permissions also need a licensed firm role, whatever the matter role.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

# Building blocks -------------------------------------------------------------
PLATFORM = ["profile.read", "profile.update", "users.picker", "teams.read"]
BROWSE = ["matters.list", "collections.list"]
MATTER_READ = ["matters.read", "members.read", "collections.read", "documents.read", "documents.download"]
MATTER_WRITE = ["matters.update", "collections.create", "collections.update", "collections.link",
                "documents.upload", "members.validate", "members.grant"]
MATTER_OWNER = ["matters.close", "matters.delete", "documents.delete", "collections.delete",
                "collections.unlink", "collections.share", "members.manage"]
ATTORNEY_ONLY = ["drafts.approve"]
CREATE = ["matters.create", "collections.create_unlinked"]

ALL_PERMISSIONS = sorted(set(PLATFORM + BROWSE + MATTER_READ + MATTER_WRITE + MATTER_OWNER + ATTORNEY_ONLY + CREATE))

# Firm roles (IAM console) ----------------------------------------------------
FIRM_ROLES: dict[str, dict] = {
    "MANAGING_ATTORNEY": {"name": "Managing Attorney", "ceiling": True, "licensed": True,
                          "permissions": PLATFORM + BROWSE + MATTER_READ + MATTER_WRITE + MATTER_OWNER + ATTORNEY_ONLY + CREATE},
    "FIRM_VIEWER": {"name": "Firm Viewer", "ceiling": True, "licensed": False,
                    "permissions": PLATFORM + BROWSE + MATTER_READ},
    "ATTORNEY": {"name": "Attorney", "ceiling": False, "licensed": True,
                 "permissions": PLATFORM + BROWSE + CREATE},
    "CASE_MANAGER": {"name": "Case Manager", "ceiling": False, "licensed": False,
                     "permissions": PLATFORM + BROWSE + CREATE},
    "PARALEGAL": {"name": "Paralegal", "ceiling": False, "licensed": False,
                  "permissions": PLATFORM + BROWSE},
}

# Matter roles ------------------------------------------------------------------
MATTER_ROLES: dict[str, dict] = {
    "LEAD": {"name": "Lead", "permissions": MATTER_READ + MATTER_WRITE + MATTER_OWNER + ATTORNEY_ONLY},
    "EDITOR": {"name": "Editor", "permissions": MATTER_READ + MATTER_WRITE},
    "VIEWER": {"name": "Viewer", "permissions": MATTER_READ},
}
BLOCKED = "BLOCKED"
MATTER_RANK = {"VIEWER": 1, "EDITOR": 2, "LEAD": 3}

# Who may hand out which matter role (SLR's GRANTS_BY_PROJECT_ROLE).
# Admins and ceiling roles that hold members.manage may grant everything, walls included.
GRANTS_BY_MATTER_ROLE = {
    "LEAD": ("LEAD", "EDITOR", "VIEWER", BLOCKED),
    "EDITOR": ("VIEWER",),
    "VIEWER": (),
}
ALL_GRANTS = ("LEAD", "EDITOR", "VIEWER", BLOCKED)

# UI capability flags returned by /api/me: the frontend never sees raw permissions
# it can't use, and buttons are shown from these.
FIRM_CAPABILITY_FLAGS = {
    "matters.create": "matter_create",
    "collections.create_unlinked": "collection_create",
    "users.picker": "user_picker",
}


def firm_permissions(firm_role: str) -> set[str]:
    return set(FIRM_ROLES.get(firm_role, {}).get("permissions", []))


def matter_permissions(matter_role: str | None) -> set[str]:
    if not matter_role or matter_role == BLOCKED:
        return set()
    return set(MATTER_ROLES[matter_role]["permissions"])


def is_ceiling(firm_role: str) -> bool:
    return bool(FIRM_ROLES.get(firm_role, {}).get("ceiling"))


def is_licensed(firm_role: str) -> bool:
    return bool(FIRM_ROLES.get(firm_role, {}).get("licensed"))


def sync_permission_sets(db: Session) -> None:
    """Upsert one row per built-in role. Raises on failure; main.py refuses to start."""
    from pymicroservices.app.firm_models import PermissionSet

    existing = {row.for_role: row for row in db.scalars(select(PermissionSet))}
    rows = [(key, "firm", spec) for key, spec in FIRM_ROLES.items()] + \
           [(key, "matter", spec) for key, spec in MATTER_ROLES.items()]
    for key, layer, spec in rows:
        row = existing.get(key) or PermissionSet(for_role=key)
        row.layer = layer
        row.name = spec["name"]
        row.permissions = sorted(set(spec["permissions"]))
        row.ceiling = bool(spec.get("ceiling"))
        row.licensed = bool(spec.get("licensed"))
        row.is_system = True
        db.add(row)
    db.commit()
    count = db.query(PermissionSet).count()
    if count < len(rows):
        raise RuntimeError("permission_sets sync incomplete; refusing to start")
