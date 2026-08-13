from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, BiliAccount, ChargeRecord, DashboardShare, User
from app.routers.dashboard import issue_share_access, validate_share_access


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
        client.headers.update(
            {
                "Origin": "http://testserver",
                "X-CSRF-Token": client.cookies["csrf_token"],
            }
        )
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
    assert client.get(f"/share/{token}").status_code == 200
    unlocked = client.post(
        f"/api/share/{token}/unlock", json={"password": "share-pass-42"}
    )
    assert unlocked.status_code == 204
    response = client.get(f"/api/share/{token}")
    assert response.status_code == 200
    body = response.json()
    assert body["records"][0]["name"] not in {"Alice", "Bob"}
    assert body["records"][0]["uid"] not in {"10001", "10002"}
    assert "accounts" not in body
    assert "latest_run" not in body
    assert f"Path=/api/share/{token}" in unlocked.headers["set-cookie"]


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


def test_csv_export_neutralizes_spreadsheet_formulas(dashboard_env) -> None:
    client, factory = dashboard_env
    with factory() as db:
        user = db.query(User).filter_by(username="owner").one()
        account = db.query(BiliAccount).filter_by(user_id=user.id).one()
        db.add(
            ChargeRecord(
                user_id=user.id,
                bili_account_id=account.id,
                event_id="formula",
                supporter_uid="+10003",
                supporter_name="=HYPERLINK(\"https://example.invalid\")",
                amount=Decimal("1.00"),
                brokerage=Decimal("0.70"),
                charged_at=datetime.now(UTC),
                remark="@SUM(1+1)",
            )
        )
        db.commit()

    exported = client.get("/api/dashboard/export.csv").text
    assert "'+10003" in exported
    assert "'=HYPERLINK" in exported
    assert "'@SUM" in exported


def test_filters_and_trend_use_application_timezone(dashboard_env, monkeypatch) -> None:
    from app.settings import get_settings

    client, factory = dashboard_env
    get_settings.cache_clear()
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    with factory() as db:
        user = db.query(User).filter_by(username="owner").one()
        account = db.query(BiliAccount).filter_by(user_id=user.id).one()
        db.add_all(
            [
                ChargeRecord(
                    user_id=user.id,
                    bili_account_id=account.id,
                    event_id="before-local-midnight",
                    supporter_uid="20001",
                    supporter_name="Timezone",
                    amount=Decimal("1.00"),
                    brokerage=Decimal("0.70"),
                    charged_at=datetime(2026, 3, 31, 15, 59, tzinfo=UTC),
                ),
                ChargeRecord(
                    user_id=user.id,
                    bili_account_id=account.id,
                    event_id="after-local-midnight",
                    supporter_uid="20002",
                    supporter_name="Timezone",
                    amount=Decimal("2.00"),
                    brokerage=Decimal("1.40"),
                    charged_at=datetime(2026, 3, 31, 16, 1, tzinfo=UTC),
                ),
            ]
        )
        db.commit()

    all_data = client.get("/api/dashboard", params={"search": "Timezone"}).json()
    assert [point["date"] for point in all_data["trend"]] == ["2026-03-31", "2026-04-01"]
    filtered = client.get(
        "/api/dashboard",
        params={"search": "Timezone", "start": "2026-04-01T00:00"},
    ).json()
    assert filtered["summary"]["total_amount"] == "2.00"
    assert filtered["records"][0]["charged_at"].endswith("+00:00")
    get_settings.cache_clear()


def test_share_access_signature_has_server_side_expiry(dashboard_env) -> None:
    client, factory = dashboard_env
    created = client.post("/api/dashboard/shares", json={}).json()
    token = created["token"]
    with factory() as db:
        user_id = client.get("/api/auth/me").json()["id"]
        share = db.query(DashboardShare).filter_by(user_id=user_id).one()
        now = datetime.now(UTC)
        access = issue_share_access(token, share, now)
        assert validate_share_access(token, share, access, now + timedelta(minutes=59))
        assert not validate_share_access(token, share, access, now + timedelta(hours=1))
        assert not validate_share_access(token + "tampered", share, access, now)


def test_share_unlock_is_rate_limited_by_client_and_token(dashboard_env) -> None:
    client, _ = dashboard_env
    first = client.post(
        "/api/dashboard/shares", json={"password": "first-share-pass-42"}
    ).json()
    path = f"/api/share/{first['token']}/unlock"
    for _ in range(5):
        response = client.post(path, json={"password": "incorrect"})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_share_password"
    limited = client.post(path, json={"password": "incorrect"})
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "rate_limited"
    assert limited.headers["retry-after"] == "300"

    second = client.post(
        "/api/dashboard/shares", json={"password": "second-share-pass-42"}
    ).json()
    other_token = client.post(
        f"/api/share/{second['token']}/unlock", json={"password": "incorrect"}
    )
    assert other_token.status_code == 401
