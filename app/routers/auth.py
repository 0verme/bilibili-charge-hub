from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from app.auth import AdminUser, CurrentUser, DbSession, SessionToken
from app.models import User, UserRole, UserSession
from app.schemas import Credentials, UserCreate, UserView
from app.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    session_expiry,
    verify_password,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"])


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "session_token",
        token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
    )


@router.post("/setup", response_model=UserView, status_code=status.HTTP_201_CREATED)
def setup_admin(payload: Credentials, response: Response, db: DbSession) -> User:
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(status.HTTP_409_CONFLICT, "system is already initialized")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.flush()
    token = new_session_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=session_expiry(),
        )
    )
    db.commit()
    set_session_cookie(response, token)
    return user


@router.post("/login", response_model=UserView)
def login(payload: Credentials, response: Response, db: DbSession) -> User:
    user = db.scalar(select(User).where(User.username == payload.username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    token = new_session_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=session_expiry(),
        )
    )
    db.commit()
    set_session_cookie(response, token)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: DbSession,
    session_token: SessionToken = None,
) -> None:
    if session_token:
        stored = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_session_token(session_token))
        )
        if stored:
            db.delete(stored)
            db.commit()
    response.delete_cookie("session_token")


@router.get("/me", response_model=UserView)
def me(user: CurrentUser) -> User:
    return user


@users_router.post("", response_model=UserView, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    _: AdminUser,
    db: DbSession,
) -> User:
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@users_router.get("", response_model=list[UserView])
def list_users(
    _: AdminUser,
    db: DbSession,
) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at)).all())
