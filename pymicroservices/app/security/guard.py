"""The central guard. Every /api route must depend on exactly one of:

  public()                      auth endpoints (rate-limited) and token-protected file links
  require_admin()               Admin console only
  require(perm, matter=..., collection=..., document=...)
                                IAM console users, and Admin console users (who pass everything)

main.py walks every route at startup and refuses to boot if one has no guard
(SLR relied on each handler calling the check by hand).

Which session is used: the frontend sends `X-Console: admin` from the Admin console,
otherwise the IAM session cookie is used. The two cookies never mix."""
import hmac
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import ApiError, forbidden, not_found, unauthorized
from pymicroservices.app.firm_models import Admin, AuthSession, Collection, IamUser
from pymicroservices.app.models import UnstructuredData
from pymicroservices.app.security import access, crypto
from pymicroservices.app.security.access import Principal
from pymicroservices.app.security.providers import get_provider
from pymicroservices.app.services.collections.collection_files import load_document
from pymicroservices.app.security.sessions import CSRF_COOKIE, CSRF_HEADER, client_ip, load_session, needs_reverify, revoke

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Ctx:
    who: Principal
    session: AuthSession
    ip: str
    perms: set[str] | None = None
    matter_role: str | None = None
    matter: object | None = None
    collection: Collection | None = None
    document: UnstructuredData | None = None  # a collection file (services/collections)


def _check_csrf(request: Request) -> None:
    if request.method in UNSAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get(CSRF_HEADER)
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise ApiError(403, "CSRF_FAILED", "Your session needs a refresh. Reload the page and try again.")


async def resolve(request: Request, db: Session, console: str | None = None) -> Ctx:
    console = console or ("admin" if request.headers.get("x-console") == "admin" else "iam")
    row = load_session(db, request, console)
    if not row:
        raise unauthorized()
    if console == "admin":
        person = db.get(Admin, row.principal_id)
        firm_role = None
    else:
        person = db.get(IamUser, row.principal_id)
        firm_role = person.firm_role if person else None
    if not person or person.status != "active" or person.firm_id != row.firm_id:
        revoke(db, row)
        db.commit()
        raise unauthorized()
    if needs_reverify(row):
        refresh = crypto.decrypt(row.refresh_token_enc) if row.refresh_token_enc else None
        ok, new_refresh = await get_provider(console).reverify(db, person.email, refresh)
        if ok and new_refresh and new_refresh != refresh:
            row.refresh_token_enc = crypto.encrypt(new_refresh)  # WorkOS rotates refresh tokens
        if not ok:
            revoke(db, row)
            db.commit()
            raise unauthorized("Your account was signed out. Please sign in again.")
        from datetime import datetime, timezone
        row.last_verified_at = datetime.now(timezone.utc)
        db.commit()
    _check_csrf(request)
    who = Principal(console=console, id=person.id, firm_id=person.firm_id, email=person.email,
                    name=person.name, firm_role=firm_role)
    return Ctx(who=who, session=row, ip=client_ip(request))


def _mark(fn, kind: str, detail: str = ""):
    fn.__lx_guard__ = (kind, detail)
    return fn


def public(reason: str):
    async def dep():
        return None
    return _mark(dep, "public", reason)


def require_admin():
    async def dep(request: Request, db: Session = Depends(get_db)) -> Ctx:
        if request.headers.get("x-console") != "admin":
            raise forbidden("Use the Admin console for this.")
        return await resolve(request, db, "admin")
    return _mark(dep, "admin")


def require_session():
    """Signed in to either console; no permission needed (e.g. /me, logout)."""
    async def dep(request: Request, db: Session = Depends(get_db)) -> Ctx:
        return await resolve(request, db)
    return _mark(dep, "session")


def require(perm: str, *, matter: str | None = None, collection: str | None = None, document: str | None = None):
    """Route gate + object check in one. `matter`/`collection`/`document` name the path
    parameter that identifies the object; the check is then scoped to that object."""
    async def dep(request: Request, db: Session = Depends(get_db)) -> Ctx:
        ctx = await resolve(request, db)
        params = request.path_params
        if matter:
            ctx.matter, ctx.perms, ctx.matter_role = access.authorize_matter(db, ctx.who, perm, params[matter])
        elif collection:
            ctx.collection, ctx.perms = access.authorize_collection(db, ctx.who, perm, params[collection])
        elif document:
            doc = load_document(db, params[document])
            if not doc:
                raise not_found("Document")
            # Bind document -> its collection (the folder has the collection's id), then check the
            # collection, which also checks the firm (no id pairing tricks).
            ctx.collection, ctx.perms = access.authorize_collection(db, ctx.who, perm, str(doc.thread_id))
            ctx.document = doc
        else:
            if perm in access.MATTER_SCOPED:
                raise RuntimeError(f"{perm} is matter-scoped; pass matter=, collection= or document=")
            access.authorize_firm(ctx.who, perm)
        return ctx
    return _mark(dep, "perm", perm)


def api_routes(app) -> list:
    """Every /api route with its full path. FastAPI keeps included routers as wrappers in app.routes,
    so walk the effective routes instead of app.routes."""
    from fastapi.routing import APIRoute, iter_route_contexts

    return [ctx for ctx in iter_route_contexts(app.routes)
            if isinstance(ctx.original_route, APIRoute) and (ctx.path or "").startswith("/api")]


def assert_all_routes_guarded(app) -> None:
    def guarded(dependant) -> bool:
        for sub in dependant.dependencies:
            if getattr(sub.call, "__lx_guard__", None) or guarded(sub):
                return True
        return False

    routes = api_routes(app)
    if not routes:
        raise RuntimeError("Found no /api routes to check; the route walk is broken.")
    missing = [f"{sorted(r.methods)} {r.path}" for r in routes if not guarded(r.original_route.dependant)]
    if missing:
        raise RuntimeError("Routes without an access guard: " + ", ".join(missing))
