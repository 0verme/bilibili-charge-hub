import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.bilibili.client import (
    CHARGE_RECORDS_URL,
    BilibiliAuthenticationError,
    BilibiliClient,
    BilibiliSchemaChanged,
    ChargePage,
)
from app.crypto import get_credential_cipher
from app.models import (
    AccountStatus,
    Base,
    BiliAccount,
    ChargeRecord,
    JobRun,
    RunStatus,
    User,
    UserRole,
)
from app.security import hash_password
from app.services.collection import ChargeCollectionService, parse_charge_time, stable_event_id


class PagedChargeClient:
    def __init__(self) -> None:
        self.requested_pages: list[int] = []

    async def fetch_charge_page(
        self, cookie_header: str, page: int, page_size: int
    ) -> ChargePage:
        assert "fake-cookie" in cookie_header
        assert page_size == 50
        self.requested_pages.append(page)
        items = {
            1: [
                {
                    "mid": "11",
                    "name": "Alice",
                    "originalThirdCoin": "10.50",
                    "brokerage": "7.00",
                    "ctime": "2026-08-12T12:00:00+00:00",
                }
            ],
            2: [
                {
                    "mid": "12",
                    "name": "Bob",
                    "originalThirdCoin": 20,
                    "brokerage": 14,
                    "ctime": 1786539600,
                }
            ],
        }.get(page, [])
        return ChargePage(items=items, has_more=page == 1)


class ExpiredChargeClient:
    async def fetch_charge_page(
        self, cookie_header: str, page: int, page_size: int
    ) -> ChargePage:
        raise BilibiliAuthenticationError("expired")


@pytest.fixture
def collection_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db


def make_account(db: Session) -> BiliAccount:
    user = User(
        username="collector",
        password_hash=hash_password("collector-password-42"),
        role=UserRole.USER,
    )
    db.add(user)
    db.flush()
    account = BiliAccount(
        user_id=user.id,
        bili_uid="123456",
        encrypted_cookie=get_credential_cipher().encrypt("SESSDATA=fake-cookie"),
    )
    db.add(account)
    db.commit()
    return account


def test_collection_paginates_and_is_idempotent(collection_db: Session) -> None:
    account = make_account(collection_db)
    client = PagedChargeClient()
    service = ChargeCollectionService(client)  # type: ignore[arg-type]

    first = asyncio.run(service.collect(collection_db, account))
    second = asyncio.run(service.collect(collection_db, account))

    assert first.pages == 2
    assert first.seen == 2
    assert first.inserted == 2
    assert second.inserted == 0
    assert client.requested_pages == [1, 2, 1, 2]
    assert collection_db.scalar(select(func.count()).select_from(ChargeRecord)) == 2
    runs = list(collection_db.scalars(select(JobRun).order_by(JobRun.started_at)))
    assert [run.status for run in runs] == [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED]
    assert all(run.finished_at and run.duration_ms is not None for run in runs)


def test_authentication_failure_marks_account_and_run(collection_db: Session) -> None:
    account = make_account(collection_db)
    service = ChargeCollectionService(ExpiredChargeClient())  # type: ignore[arg-type]

    with pytest.raises(BilibiliAuthenticationError):
        asyncio.run(service.collect(collection_db, account))

    collection_db.refresh(account)
    run = collection_db.scalar(select(JobRun))
    assert account.status == AccountStatus.EXPIRED
    assert run is not None and run.status == RunStatus.FAILED
    assert run.error == "Bilibili account authentication expired"


def test_event_id_prefers_source_id_and_has_stable_fallback() -> None:
    assert stable_event_id("account", {"id": "source-1"}) == stable_event_id(
        "account", {"id": "source-1", "name": "changed"}
    )
    item = {
        "mid": "11",
        "name": "Alice",
        "originalThirdCoin": "10.50",
        "ctime": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
    }
    assert stable_event_id("account", item) == stable_event_id("account", dict(item))


def test_charge_time_treats_naive_bilibili_time_as_shanghai_local_time() -> None:
    expected = datetime(2026, 8, 13, 14, 29, 31, tzinfo=UTC)

    assert parse_charge_time("2026-08-13 22:29:31") == expected
    assert parse_charge_time("2026-08-13T22:29:31+08:00") == expected
    assert parse_charge_time("2026-08-13T14:29:31Z") == expected


def test_bilibili_client_rejects_changed_charge_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(CHARGE_RECORDS_URL)
        return httpx.Response(200, json={"code": 0, "data": {"unexpected": []}})

    async def exercise() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as raw:
            client = BilibiliClient(raw)
            with pytest.raises(BilibiliSchemaChanged):
                await client.fetch_charge_page("SESSDATA=masked", 1)

    asyncio.run(exercise())
