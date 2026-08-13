from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, User, UserRole
from app.security import hash_password


def enable_browser_writes(client: TestClient) -> None:
    client.headers.update(
        {
            "Origin": "http://testserver",
            "X-CSRF-Token": client.cookies["csrf_token"],
        }
    )


@pytest.fixture
def db_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(db_factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with db_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


def test_setup_login_logout_flow(client: TestClient, db_factory: sessionmaker[Session]) -> None:
    credentials = {"username": "owner", "password": "12345"}
    response = client.post("/api/auth/setup", json=credentials)

    assert response.status_code == 201
    assert response.json()["role"] == "admin"
    assert "session_token" in client.cookies
    enable_browser_writes(client)
    with db_factory() as db:
        stored = db.scalar(select(User).where(User.username == "owner"))
        assert stored is not None
        assert stored.password_hash != credentials["password"]

    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    unauthenticated = client.get("/api/auth/me")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "auth_required"

    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200
    assert response.json()["username"] == "owner"


def test_setup_is_one_time_and_admin_can_create_user(client: TestClient) -> None:
    owner = {"username": "owner", "password": "correct-horse-42"}
    assert client.post("/api/auth/setup", json=owner).status_code == 201
    enable_browser_writes(client)
    assert client.post("/api/auth/setup", json=owner).status_code == 409

    response = client.post(
        "/api/users",
        json={"username": "member", "password": "member-password-42", "role": "user"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == UserRole.USER
    assert len(client.get("/api/users").json()) == 2


def test_auth_pages_route_by_initialization_and_session_state(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"

    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    assert "创建首位管理员" in setup_page.text
    assert "操作" not in setup_page.text

    credentials = {"username": "owner", "password": "correct-horse-42"}
    assert client.post("/api/auth/setup", json=credentials).status_code == 201

    for path in ("/login", "/setup"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"

    client.cookies.clear()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "登录管理后台" in login_page.text
    assert "首次初始化管理员" not in login_page.text

    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_setup_reopens_when_all_admins_are_disabled(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    credentials = {"username": "owner", "password": "correct-horse-42"}
    assert client.post("/api/auth/setup", json=credentials).status_code == 201
    with db_factory() as db:
        owner = db.scalar(select(User).where(User.username == "owner"))
        assert owner is not None
        owner.is_active = False
        db.commit()
    client.cookies.clear()

    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"

    duplicate = client.post("/api/auth/setup", json=credentials)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "username_exists"

    recovered = client.post(
        "/api/auth/setup",
        json={"username": "recovery-admin", "password": "new-admin-password-42"},
    )
    assert recovered.status_code == 201
    assert recovered.json()["role"] == UserRole.ADMIN


def test_setup_is_available_when_only_regular_users_exist(
    client: TestClient, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as db:
        db.add(
            User(
                username="member",
                password_hash=hash_password("member-password-42"),
                role=UserRole.USER,
            )
        )
        db.commit()

    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert (
        client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "correct-horse-42"},
        ).status_code
        == 201
    )


def test_invalid_login_does_not_reveal_which_field_failed(client: TestClient) -> None:
    client.post(
        "/api/auth/setup",
        json={"username": "owner", "password": "correct-horse-42"},
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "owner", "password": "incorrect-pass-42"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "invalid_credentials",
        "message": "invalid credentials",
    }


def test_invalid_session_uses_session_expired_error_code(client: TestClient) -> None:
    client.cookies.set("session_token", "invalid-session-token")

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "session_expired"


def test_core_schema_contains_all_required_tables(db_factory: sessionmaker[Session]) -> None:
    expected = {
        "users",
        "user_sessions",
        "bili_accounts",
        "qr_login_sessions",
        "charge_records",
        "schedule_jobs",
        "job_runs",
        "notification_channels",
        "notification_subscriptions",
        "notification_outbox",
        "notification_deliveries",
        "coupon_claims",
    }

    assert expected <= set(Base.metadata.tables)
