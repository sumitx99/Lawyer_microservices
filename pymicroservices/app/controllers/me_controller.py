"""Who am I, people picker, my sessions."""
from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.firm_db import get_db
from pymicroservices.app.utilities.errors import not_found
from pymicroservices.app.firm_models import Admin, AuthSession, IamUser, MatterMember, Team, TeamMember
from pymicroservices.app.security import access, sessions
from pymicroservices.app.security import permissions as P
from pymicroservices.app.security.guard import Ctx, require, require_session
from pymicroservices.app.services.firm import audit
from pymicroservices.app.services.firm.serialize import iso, principal_out

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
def me(db: Session = Depends(get_db), ctx: Ctx = Depends(require_session())):
    model = Admin if ctx.who.is_admin else IamUser
    return {"principal": principal_out(db, ctx.who.console, db.get(model, ctx.who.id))}


@router.get("/me/sessions")
def my_sessions(db: Session = Depends(get_db), ctx: Ctx = Depends(require_session())):
    rows = db.scalars(select(AuthSession).where(AuthSession.console == ctx.who.console,
                                                AuthSession.principal_id == ctx.who.id,
                                                AuthSession.revoked_at.is_(None))
                      .order_by(AuthSession.last_seen_at.desc())).all()
    return {"items": [{"id": r.id, "ip": r.ip, "user_agent": r.user_agent, "created_at": iso(r.created_at),
                       "last_seen_at": iso(r.last_seen_at), "current": r.id == ctx.session.id} for r in rows]}


@router.delete("/me/sessions/{session_id}")
def end_my_session(session_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(require_session())):
    row = db.get(AuthSession, session_id)
    if not row or row.principal_id != ctx.who.id or row.console != ctx.who.console:
        raise not_found("Session")
    sessions.revoke(db, row)
    audit.record_ctx(db, ctx, "session.revoked", "session", row.id)
    db.commit()
    return {"status": "success"}


@router.get("/users/picker")
def user_picker(q: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(require("users.picker"))):
    """People you can add to a matter. Admins and ceiling roles see the whole firm; everyone
    else sees people they share a team or a matter with (SLR listed every user to everyone)."""
    stmt = select(IamUser).where(IamUser.firm_id == ctx.who.firm_id, IamUser.status != "deactivated")
    if not (ctx.who.is_admin or P.is_ceiling(ctx.who.firm_role)):
        my_teams = select(TeamMember.team_id).where(TeamMember.user_id == ctx.who.id)
        teammates = select(TeamMember.user_id).where(TeamMember.team_id.in_(my_teams))
        my_matters = access.visible_matter_ids(ctx.who)
        colleagues = select(MatterMember.principal_id).where(MatterMember.matter_id.in_(my_matters),
                                                             MatterMember.principal_type == "user")
        stmt = stmt.where(or_(IamUser.id.in_(teammates), IamUser.id.in_(colleagues)))
    if q:
        stmt = stmt.where(or_(IamUser.name.ilike(f"%{q}%"), IamUser.email.ilike(f"%{q}%")))
    users = db.scalars(stmt.order_by(IamUser.name).limit(50)).all()
    teams = db.scalars(select(Team).where(Team.firm_id == ctx.who.firm_id, Team.name.ilike(f"%{q}%"))
                       .order_by(Team.name).limit(20)).all()
    return {"users": [{"id": u.id, "name": u.name, "email": u.email, "firm_role": u.firm_role} for u in users],
            "teams": [{"id": t.id, "name": t.name} for t in teams]}
