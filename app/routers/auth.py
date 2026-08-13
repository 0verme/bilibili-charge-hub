from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.auth import AdminUser, CurrentUser, DbSession, SessionToken, has_active_admin
from app.errors import raise_api_error
from app.models import User, UserRole, UserSession
from app.schemas import Credentials, PasswordChange, PasswordReset, UserCreate, UserUpdate, UserView
from app.security import (
    hash_password,
    hash_session_token,
    new_csrf_token,
    new_session_token,
    session_expiry,
    verify_password,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"])


def set_session_cookie(response: Response, token: str, csrf_token: str) -> None:
    response.set_cookie(
        "session_token",
        token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        max_age=7 * 24 * 60 * 60,
        secure=get_settings().app_env == "production",
        samesite="strict",
    )


@router.post("/setup", response_model=UserView, status_code=status.HTTP_201_CREATED)
def setup_admin(payload: Credentials, response: Response, db: DbSession) -> User:
    if has_active_admin(db):
        raise_api_error(
            status.HTTP_409_CONFLICT,
            "already_initialized",
            "system is already initialized",
        )
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise_api_error(status.HTTP_409_CONFLICT, "username_exists", "username already exists")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.flush()
    token, csrf_token = new_session_token(), new_csrf_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=session_expiry(),
        )
    )
    db.commit()
    set_session_cookie(response, token, csrf_token)
    return user


@router.post("/login", response_model=UserView)
def login(payload: Credentials, response: Response, db: DbSession) -> User:
    user = db.scalar(select(User).where(User.username == payload.username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise_api_error(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "invalid credentials")
    token, csrf_token = new_session_token(), new_csrf_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=session_expiry(),
        )
    )
    db.commit()
    set_session_cookie(response, token, csrf_token)
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
    response.delete_cookie("csrf_token")


@router.get("/me", response_model=UserView)
def me(user: CurrentUser) -> User:
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChange,
    user: CurrentUser,
    db: DbSession,
    session_token: SessionToken = None,
) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise_api_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_current_password",
            "current password is incorrect",
        )
    user.password_hash = hash_password(payload.new_password)
    current_hash = hash_session_token(session_token) if session_token else ""
    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    for stored in sessions:
        if stored.token_hash != current_hash:
            db.delete(stored)
    db.commit()


@users_router.post("", response_model=UserView, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    _: AdminUser,
    db: DbSession,
) -> User:
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise_api_error(status.HTTP_409_CONFLICT, "username_exists", "username already exists")
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


def get_user_or_404(db: DbSession, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise_api_error(status.HTTP_404_NOT_FOUND, "user_not_found", "user not found")
    return user


@users_router.patch("/{user_id}", response_model=UserView)
def update_user(user_id: str, payload: UserUpdate, admin: AdminUser, db: DbSession) -> User:
    target = get_user_or_404(db, user_id)
    if target.id == admin.id and payload.is_active is False:
        raise_api_error(
            status.HTTP_409_CONFLICT,
            "cannot_disable_self",
            "cannot disable current administrator",
        )
    target.is_active = payload.is_active
    if not payload.is_active:
        for stored in db.scalars(select(UserSession).where(UserSession.user_id == target.id)):
            db.delete(stored)
    db.commit()
    return target


@users_router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(user_id: str, payload: PasswordReset, _: AdminUser, db: DbSession) -> None:
    target = get_user_or_404(db, user_id)
    target.password_hash = hash_password(payload.new_password)
    for stored in db.scalars(select(UserSession).where(UserSession.user_id == target.id)):
        db.delete(stored)
    db.commit()
