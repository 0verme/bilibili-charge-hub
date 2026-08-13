from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, User, UserSession
from app.routers.dashboard import local_period_boundaries
from app.services.coupon import local_claim_month


@pytest.fixture
def hardened_client() -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "correct-horse-42"},
        )
        assert response.status_code == 201
        yield client, factory


def test_browser_writes_require_same_origin_and_csrf(hardened_client) -> None:
    client, _ = hardened_client
    payload = {"username": "member", "password": "member-password-42", "role": "user"}
    assert client.post(
        "/api/users", json=payload, headers={"Origin": "http://evil.example"}
    ).status_code == 403
    assert client.post(
        "/api/users", json=payload, headers={"Origin": "http://testserver"}
    ).status_code == 403
    response = client.post(
        "/api/users",
        json=payload,
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": client.cookies["csrf_token"],
        },
    )
    assert response.status_code == 201


def test_password_change_revokes_other_sessions(hardened_client) -> None:
    client, factory = hardened_client
    second = TestClient(client.app)
    assert second.post(
        "/api/auth/login",
        json={"username": "owner", "password": "correct-horse-42"},
    ).status_code == 200
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "correct-horse-42", "new_password": "changed-horse-84"},
    )
    assert response.status_code == 204
    assert second.get("/api/auth/me").status_code == 401
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "owner"))
        assert user is not None
        assert len(list(db.scalars(select(UserSession).where(UserSession.user_id == user.id)))) == 1


def test_shanghai_boundaries_use_local_calendar_month(monkeypatch) -> None:
    from app.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    instant = datetime(2026, 3, 31, 17, 0, tzinfo=UTC)  # April 1, 01:00 Shanghai.
    today, month = local_period_boundaries(instant)
    assert today == datetime(2026, 3, 31, 16, 0, tzinfo=UTC)
    assert month == datetime(2026, 3, 31, 16, 0, tzinfo=UTC)
    assert local_claim_month(instant) == "2026-04"
    get_settings.cache_clear()


def test_dashboard_template_does_not_use_unsafe_dom_rendering() -> None:
    source = ("app/templates/dashboard.html", "app/static/dashboard.js")
    for path in source:
        content = __import__("pathlib").Path(path).read_text(encoding="utf-8")
        assert "innerHTML" not in content
        assert "cdn.jsdelivr" not in content
