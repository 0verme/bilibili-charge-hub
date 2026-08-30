from datetime import UTC, datetime
from typing import Annotated, TypeAlias

from fastapi import Cookie, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import raise_api_error
from app.models import User, UserRole, UserSession
from app.security import hash_session_token

DbSession: TypeAlias = Annotated[Session, Depends(get_db)]
SessionToken: TypeAlias = Annotated[str | None, Cookie()]


def has_active_admin(db: Session) -> bool:
    return (
        db.scalar(
            select(User.id).where(User.role == UserRole.ADMIN, User.is_active.is_(True)).limit(1)
        )
        is not None
    )


def get_optional_current_user(db: Session, session_token: str | None) -> User | None:
    if not session_token:
        return None
    session = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
    )
    now = datetime.now(UTC)
    if session is None or session.expires_at.replace(tzinfo=UTC) <= now:
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def get_current_user(db: DbSession, session_token: SessionToken = None) -> User:
    if not session_token:
        raise_api_error(status.HTTP_401_UNAUTHORIZED, "auth_required", "authentication required")
    session = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
    )
    now = datetime.now(UTC)
    if session is None or session.expires_at.replace(tzinfo=UTC) <= now:
        raise_api_error(status.HTTP_401_UNAUTHORIZED, "session_expired", "session expired")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise_api_error(status.HTTP_401_UNAUTHORIZED, "account_disabled", "account disabled")
    session.last_seen_at = now
    db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN:
        raise_api_error(status.HTTP_403_FORBIDDEN, "admin_required", "administrator required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
