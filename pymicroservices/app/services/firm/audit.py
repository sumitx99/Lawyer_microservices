from sqlalchemy.orm import Session

from pymicroservices.app.firm_models import AuditEvent


def record(db: Session, action: str, *, firm_id: str | None, actor_type: str, actor_id: str | None = None,
           target_type: str | None = None, target_id: str | None = None, ip: str | None = None,
           data: dict | None = None) -> None:
    """Add an audit row to the current transaction (committed with the change it describes)."""
    db.add(AuditEvent(firm_id=firm_id, actor_type=actor_type, actor_id=actor_id, action=action,
                      target_type=target_type, target_id=target_id, ip=ip, data=data or {}))


def record_ctx(db: Session, ctx, action: str, target_type: str | None = None, target_id: str | None = None,
               **data) -> None:
    record(db, action, firm_id=ctx.who.firm_id, actor_type=ctx.who.console, actor_id=ctx.who.id,
           target_type=target_type, target_id=target_id, ip=ctx.ip, data=data)
