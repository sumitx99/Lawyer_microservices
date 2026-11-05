"""Server-side sessions in Postgres, one cookie per console.

Cookie names differ per console (SLR used one name for both, so signing in to one
console logged you out of the other). Revoking a row ends the session immediately."""
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.secrets_control import get_settings
from pymicroservices.app.firm_models import AuthSession
from pymicroservices.app.security import crypto

SESSION_COOKIES = {"iam": "lx_session", "admin": "lx_admin_session"}
CSRF_COOKIE = "lx_csrf"
CSRF_HEADER = "x-csrf-token"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cookie_kwargs() -> dict:
    s = get_settings()
    return {"secure": s.cookie_secure, "samesite": "lax", "domain": s.cookie_domain, "path": "/"}


def set_csrf_cookie(response: Response) -> str:
    token = crypto.random_token(24)
    response.set_cookie(CSRF_COOKIE, token, httponly=False, max_age=get_settings().session_absolute_hours * 3600,
                        **_cookie_kwargs())
    return token


def create_session(db: Session, request: Request, response: Response, console: str, principal_id: str,
                   firm_id: str, refresh_token: str | None, provider_session_id: str | None = None) -> AuthSession:
    s = get_settings()
    token = crypto.random_token()
    now = _now()
    row = AuthSession(token_hash=crypto.sha256(token), console=console, principal_id=principal_id, firm_id=firm_id,
                      refresh_token_enc=crypto.encrypt(refresh_token) if refresh_token else None,
                      provider_session_id=provider_session_id,
                      ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:400],
                      created_at=now, last_seen_at=now, last_verified_at=now,
                      expires_at=now + timedelta(hours=s.session_absolute_hours))
    db.add(row)
    db.flush()
    response.set_cookie(SESSION_COOKIES[console], token, httponly=True,
                        max_age=s.session_absolute_hours * 3600, **_cookie_kwargs())
    set_csrf_cookie(response)
    return row


def load_session(db: Session, request: Request, console: str) -> AuthSession | None:
    token = request.cookies.get(SESSION_COOKIES[console])
    if not token:
        return None
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == crypto.sha256(token),
                                              AuthSession.console == console))
    if not row or row.revoked_at is not None:
        return None
    now = _now()
    idle = timedelta(minutes=get_settings().session_idle_minutes)
    if _aware(row.expires_at) <= now or _aware(row.last_seen_at) + idle <= now:
        row.revoked_at = now
        db.commit()
        return None
    if now - _aware(row.last_seen_at) > timedelta(seconds=30):
        row.last_seen_at = now
        db.commit()
    return row


def needs_reverify(row: AuthSession) -> bool:
    return _now() - _aware(row.last_verified_at) > timedelta(minutes=get_settings().session_reverify_minutes)


def revoke(db: Session, row: AuthSession) -> None:
    row.revoked_at = _now()


def revoke_all(db: Session, console: str, principal_id: str) -> int:
    result = db.execute(update(AuthSession)
                        .where(AuthSession.console == console, AuthSession.principal_id == principal_id,
                               AuthSession.revoked_at.is_(None))
                        .values(revoked_at=_now()))
    return result.rowcount or 0


def clear_cookies(response: Response, console: str) -> None:
    response.delete_cookie(SESSION_COOKIES[console], **_cookie_kwargs())
