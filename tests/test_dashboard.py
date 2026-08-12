from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, BiliAccount, ChargeRecord, User


@pytest.fixture
def dashboard_env() -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
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
        client.post("/api/auth/setup", json={"username": "owner", "password": "correct-horse-42"})
        with factory() as db:
            user = db.query(User).filter_by(username="owner").one()
            account = BiliAccount(user_id=user.id, bili_uid="123", encrypted_cookie="cipher")
            db.add(account)
            db.flush()
            db.add_all(
                [
                    ChargeRecord(
                        user_id=user.id,
                        bili_account_id=account.id,
                        event_id="a",
                        supporter_uid="10001",
                        supporter_name="Alice",
                        amount=Decimal("10.00"),
                        brokerage=Decimal("7.00"),
                        charged_at=datetime.now(UTC),
                    ),
                    ChargeRecord(
                        user_id=user.id,
                        bili_account_id=account.id,
                        event_id="b",
                        supporter_uid="10002",
                        supporter_name="Bob",
                        amount=Decimal("20.00"),
                        brokerage=Decimal("14.00"),
                        charged_at=datetime.now(UTC),
                    ),
                ]
            )
            db.commit()
        yield client, factory


def test_dashboard_summary_filter_pagination_and_csv(dashboard_env) -> None:
    client, _ = dashboard_env
    data = client.get("/api/dashboard", params={"search": "Alice", "page_size": 1}).json()
    assert data["summary"]["total_amount"] == "10.00"
    assert data["summary"]["platform_difference"] == "3.00"
    assert data["pagination"]["total"] == 1
    assert data["records"][0]["name"] == "Alice"
    csv_response = client.get("/api/dashboard/export.csv")
    assert csv_response.status_code == 200
    assert "Alice" in csv_response.text and "Bob" in csv_response.text


def test_share_is_random_expiring_password_protected_and_masked(dashboard_env) -> None:
    client, _ = dashboard_env
    created = client.post("/api/dashboard/shares", json={"password": "share-pass-42"}).json()
    token = created["token"]
    assert len(token) > 30
    assert client.get(f"/api/share/{token}").status_code == 401
    response = client.get(f"/api/share/{token}", params={"password": "share-pass-42"})
    assert response.status_code == 200
    body = response.json()
    assert body["records"][0]["name"] not in {"Alice", "Bob"}
    assert body["records"][0]["uid"] not in {"10001", "10002"}


def test_dashboard_never_returns_other_tenant_records(dashboard_env) -> None:
    client, factory = dashboard_env
    with factory() as db:
        other = User(username="other", password_hash="not-used")
        db.add(other)
        db.flush()
        account = BiliAccount(user_id=other.id, bili_uid="999", encrypted_cookie="cipher")
        db.add(account)
        db.flush()
        db.add(
            ChargeRecord(
                user_id=other.id,
                bili_account_id=account.id,
                event_id="secret",
                supporter_uid="99999",
                supporter_name="Secret",
                amount=Decimal("999.00"),
                brokerage=Decimal("0"),
                charged_at=datetime.now(UTC),
            )
        )
        db.commit()
    text = str(client.get("/api/dashboard").json())
    assert "Secret" not in text and "999.00" not in text
