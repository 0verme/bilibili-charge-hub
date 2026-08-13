from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bilibili.client import (
    BilibiliAuthenticationError,
    QrCode,
    QrPollResult,
    get_bilibili_client,
)
from app.crypto import get_credential_cipher
from app.database import get_db
from app.main import create_app
from app.models import Base, BiliAccount, JobKind, QrLoginSession, ScheduleJob, User, UserRole
from app.security import hash_password


class FakeBilibiliClient:
    async def generate_qr(self) -> QrCode:
        return QrCode(key="qr-key-1", url="https://example.invalid/qr/1")

    async def poll_qr(self, key: str) -> QrPollResult:
        assert key == "qr-key-1"
        return QrPollResult(
            state="completed",
            message="ok",
            cookies={
                "DedeUserID": "123456",
                "SESSDATA": "test-session-secret",
                "bili_jct": "test-csrf-secret",
            },
            refresh_token="test-refresh-secret",
        )

    async def close(self) -> None:
        return None


class ExpiredBilibiliClient:
    async def fetch_charge_page(self, cookie_header: str, page: int, page_size: int):
        raise BilibiliAuthenticationError("expired")

    async def close(self) -> None:
        return None


@pytest.fixture
def account_db_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def account_client(
    account_db_factory: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with account_db_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_bilibili_client] = FakeBilibiliClient
    with TestClient(app) as client:
        client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "correct-horse-42"},
        )
        client.headers.update(
            {
                "Origin": "http://testserver",
                "X-CSRF-Token": client.cookies["csrf_token"],
            }
        )
        yield client


def test_qr_login_encrypts_credentials_and_returns_only_metadata(
    account_client: TestClient,
    account_db_factory: sessionmaker[Session],
) -> None:
    created = account_client.post("/api/bili/qr-sessions")
    assert created.status_code == 201
    assert created.json()["qr_url"] == "https://example.invalid/qr/1"

    completed = account_client.get(f"/api/bili/qr-sessions/{created.json()['id']}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    accounts = account_client.get("/api/bili/accounts").json()
    assert accounts == [
        {
            "id": completed.json()["account_id"],
            "bili_uid": "123456",
            "display_name": "",
            "status": "active",
            "last_checked_at": None,
        }
    ]
    assert "cookie" not in str(accounts).lower()
    with account_db_factory() as db:
        stored = db.scalar(select(BiliAccount))
        assert stored is not None
        assert "test-session-secret" not in stored.encrypted_cookie
        assert not hasattr(stored, "encrypted_refresh_token")
        jobs = list(db.scalars(select(ScheduleJob).where(ScheduleJob.bili_account_id == stored.id)))
        assert {job.kind for job in jobs} == {
            JobKind.CHARGE_COLLECTION,
            JobKind.COUPON_CLAIM,
        }
        collection_job = next(job for job in jobs if job.kind == JobKind.CHARGE_COLLECTION)
        coupon_job = next(job for job in jobs if job.kind == JobKind.COUPON_CLAIM)
        assert collection_job.trigger_config == {"seconds": 300}
        assert collection_job.next_run_at is not None
        assert coupon_job.trigger_config == {"expression": "0 1 * * *"}


def test_qr_sessions_and_accounts_are_tenant_isolated(
    account_client: TestClient,
    account_db_factory: sessionmaker[Session],
) -> None:
    with account_db_factory() as db:
        other = User(
            username="other",
            password_hash=hash_password("other-password-42"),
            role=UserRole.USER,
        )
        db.add(other)
        db.flush()
        other_account = BiliAccount(
            user_id=other.id,
            bili_uid="999999",
            encrypted_cookie="encrypted-placeholder",
        )
        other_qr = QrLoginSession(
            user_id=other.id,
            qrcode_key="other-key",
            qr_url="https://example.invalid/other",
            expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
        db.add_all([other_account, other_qr])
        db.commit()
        account_id = other_account.id
        qr_id = other_qr.id

    assert account_client.get("/api/bili/accounts").json() == []
    assert account_client.get(f"/api/bili/qr-sessions/{qr_id}").status_code == 404
    assert account_client.delete(f"/api/bili/accounts/{account_id}").status_code == 404


def test_bilibili_auth_expiry_uses_distinct_error_code(
    account_client: TestClient,
    account_db_factory: sessionmaker[Session],
) -> None:
    with account_db_factory() as db:
        owner = db.scalar(select(User).where(User.username == "owner"))
        assert owner is not None
        account = BiliAccount(
            user_id=owner.id,
            bili_uid="expired-account",
            encrypted_cookie=get_credential_cipher().encrypt(
                "DedeUserID=expired-account; SESSDATA=expired; bili_jct=expired"
            ),
        )
        db.add(account)
        db.commit()
        account_id = account.id
    account_client.app.dependency_overrides[get_bilibili_client] = ExpiredBilibiliClient

    response = account_client.post(f"/api/bili/accounts/{account_id}/collect")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "bili_auth_expired"
