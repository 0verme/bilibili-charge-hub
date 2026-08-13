from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, User, UserRole


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
