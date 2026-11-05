"""Rate limits stored in Postgres, so every API replica shares them (SLR kept them in
process memory, keyed on the proxy's IP)."""
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pymicroservices.app.utilities.errors import ApiError
from pymicroservices.app.firm_models import RateLimitHit
from pymicroservices.app.security.sessions import client_ip


def hit(db: Session, request: Request, bucket: str, limit: int, window_seconds: int, extra_key: str = "") -> None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=window_seconds)
    key = f"{bucket}:{client_ip(request)}:{extra_key.lower()}"
    count = db.scalar(select(func.count()).select_from(RateLimitHit)
                      .where(RateLimitHit.key == key, RateLimitHit.created_at >= since)) or 0
    if count >= limit:
        raise ApiError(429, "RATE_LIMITED", "Too many attempts. Please try again after some time.",
                       retry_after=window_seconds)
    db.add(RateLimitHit(key=key, created_at=now))
    db.execute(delete(RateLimitHit).where(RateLimitHit.created_at < now - timedelta(days=1)))
    db.commit()
