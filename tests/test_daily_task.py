import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bilibili.client import (
    BilibiliAuthenticationError,
    BilibiliClient,
    CoinOpResult,
    DailyTaskReward,
    NavInfo,
    VideoInfo,
    VideoUp,
    _sign_wbi,
)
from app.crypto import get_credential_cipher
from app.database import get_db
from app.main import create_app
from app.models import (
    AccountStatus,
    Base,
    BiliAccount,
    DailyTaskProfile,
    DailyTaskRecord,
    JobKind,
    JobRun,
    NotificationOutbox,
    RunStatus,
    ScheduleJob,
    User,
    UserRole,
)
from app.security import hash_password
from app.services.daily_task import DailyTaskService


class FakeDailyClient:
    """In-memory Bilibili client that always succeeds with two candidate videos."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reward = DailyTaskReward(login=False, watch=False, share=False, coins_exp=0)
        self.nav = NavInfo(mid="123456", uname="tester", level=5)
        self.balance = Decimal(100)
        self.today_exp = 0
        self.followings = [VideoUp(mid="111", uname="up-1")]
        self.up_videos = [
            VideoInfo(aid="1001", bvid="BV1", title="video-1", cid="c1"),
            VideoInfo(aid="1002", bvid="BV2", title="video-2", cid="c2"),
        ]
        self.ranking: list[VideoInfo] = []
        self.has_buvid3_flag = True

    async def get_daily_task_reward(self, cookie_header: str) -> DailyTaskReward:
        self.calls.append("reward")
        return self.reward

    async def get_nav(self, cookie_header: str) -> NavInfo:
        self.calls.append("nav")
        return self.nav

    async def get_coin_today_exp(self, cookie_header: str) -> int:
        self.calls.append("today_exp")
        return self.today_exp

    async def get_coin_balance(self, cookie_header: str) -> Decimal:
        self.calls.append("balance")
        return self.balance

    async def get_archive_coins(self, cookie_header: str, aid: str) -> int:
        self.calls.append(f"archive:{aid}")
        return 0

    async def add_coin(
        self,
        cookie_header: str,
        csrf: str,
        aid: str,
        bvid: str,
        multiply: int = 1,
        select_like: bool = False,
    ) -> CoinOpResult:
        self.calls.append(f"add:{aid}")
        return CoinOpResult(code=0, message="ok", success=True, retriable=True)

    async def share_video(self, cookie_header: str, csrf: str, aid: str) -> CoinOpResult:
        self.calls.append(f"share:{aid}")
        return CoinOpResult(code=0, message="ok", success=True, retriable=True)

    async def upload_heartbeat(
        self, cookie_header: str, csrf: str, video: VideoInfo, uid: str, played_time: int = 0
    ) -> CoinOpResult:
        self.calls.append("heartbeat")
        return CoinOpResult(code=0, message="ok", success=True, retriable=True)

    async def search_up_videos(
        self, cookie_header: str, mid: str, page_number: int = 1, page_size: int = 30
    ) -> tuple[list[VideoInfo], int]:
        self.calls.append(f"search:{mid}")
        return list(self.up_videos), len(self.up_videos)

    async def get_followings(
        self, cookie_header: str, uid: str, page: int = 1, page_size: int = 50
    ) -> list[VideoUp]:
        self.calls.append("followings")
        return list(self.followings)

    async def get_ranking(self) -> list[VideoInfo]:
        self.calls.append("ranking")
        return list(self.ranking)

    def has_buvid3(self, cookie_header: str) -> bool:
        return self.has_buvid3_flag

    async def close(self) -> None:
        return None


class ExpiredDailyClient(FakeDailyClient):
    async def get_daily_task_reward(self, cookie_header: str) -> DailyTaskReward:
        self.calls.append("reward")
        raise BilibiliAuthenticationError("expired")


@pytest.fixture
def daily_db_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_daily_account(
    factory: sessionmaker[Session], *, uid: str = "123456"
) -> tuple[str, str]:
    with factory() as db:
        user = User(
            username=f"daily-{uid}",
            password_hash=hash_password("daily-password-42"),
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        account = BiliAccount(
            user_id=user.id,
            bili_uid=uid,
            encrypted_cookie=get_credential_cipher().encrypt(
                "SESSDATA=fake-session; bili_jct=fake-csrf; buvid3=fake-buvid"
            ),
        )
        db.add(account)
        db.flush()
        profile = DailyTaskProfile(
            user_id=user.id,
            bili_account_id=account.id,
            enabled=True,
            target_coins=2,
            protected_coins=50,
            share_enabled=True,
            watch_enabled=False,
        )
        db.add(profile)
        db.commit()
        return account.id, user.id


def test_daily_task_donates_coins_and_shares(
    daily_db_factory: sessionmaker[Session],
) -> None:
    account_id, user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        outcome = asyncio.run(service.run(db, account))

        assert outcome.status == "success"
        record = db.get(DailyTaskRecord, outcome.record_id)
        assert record is not None
        assert record.coins_donated == 2
        assert record.share_done is True
        assert record.login_done is True
        assert len(record.donated_videos) == 2
        assert "投币" in record.message
        assert "分享" in record.message
        run = db.get(JobRun, outcome.run_id)
        assert run is not None and run.status == RunStatus.SUCCEEDED
        assert run.result["coins_donated"] == 2
        # 投币前余额被记录
        assert record.balance_before == Decimal(100)
    assert {"add:1001", "add:1002"} <= set(client.calls)
    assert any(call.startswith("share:") for call in client.calls)


def test_daily_task_is_idempotent_per_day(daily_db_factory: sessionmaker[Session]) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        first = asyncio.run(service.run(db, account))
        second = asyncio.run(service.run(db, account))

        assert first.status == "success"
        assert second.status == "skipped"
        assert db.scalar(select(func.count()).select_from(DailyTaskRecord)) == 1
        runs = list(db.scalars(select(JobRun).order_by(JobRun.started_at)))
        assert [run.status for run in runs] == [RunStatus.SUCCEEDED, RunStatus.SKIPPED]
    assert client.calls.count("add:1001") + client.calls.count("add:1002") == 2


def test_daily_task_skips_when_profile_disabled(daily_db_factory: sessionmaker[Session]) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        profile = db.scalar(
            select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account_id)
        )
        assert profile is not None
        profile.enabled = False
        db.commit()
        outcome = asyncio.run(service.run(db, account))

        assert outcome.status == "skipped"
        assert outcome.message == "每日任务未启用"
        run = db.get(JobRun, outcome.run_id)
        assert run is not None and run.status == RunStatus.SKIPPED
    assert client.calls == []


def test_daily_task_auth_failure_expires_account(
    daily_db_factory: sessionmaker[Session],
) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    service = DailyTaskService(ExpiredDailyClient())  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        job = ScheduleJob(
            user_id=account.user_id,
            bili_account_id=account.id,
            kind=JobKind.DAILY_TASK,
            trigger_type="cron",
            trigger_config={"expression": "30 1 * * *"},
        )
        db.add(job)
        db.commit()

        with pytest.raises(BilibiliAuthenticationError):
            asyncio.run(service.run(db, account))

        db.refresh(account)
        assert account.status == AccountStatus.EXPIRED
        db.refresh(job)
        assert job.enabled is False
        assert job.next_run_at is None
        events = list(db.scalars(select(NotificationOutbox)))
        assert {event.event_type for event in events} >= {"cookie_expired", "daily_task_failed"}


def test_daily_task_skips_share_without_buvid3(
    daily_db_factory: sessionmaker[Session],
) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    client.has_buvid3_flag = False
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        outcome = asyncio.run(service.run(db, account))

        record = db.get(DailyTaskRecord, outcome.record_id)
        assert record is not None
        assert record.status == "success"
        assert record.share_done is False
        assert record.coins_donated == 2
        assert "buvid3" in record.message
        assert "投币：成功" in record.message


def test_daily_task_respects_protected_coin_balance(
    daily_db_factory: sessionmaker[Session],
) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    client.balance = Decimal(30)
    client.up_videos = []
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        profile = db.scalar(
            select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account_id)
        )
        assert profile is not None
        profile.share_enabled = False
        db.commit()
        outcome = asyncio.run(service.run(db, account))

        record = db.get(DailyTaskRecord, outcome.record_id)
        assert record is not None
        assert record.status == "partial"
        assert record.coins_donated == 0
        assert record.share_done is False
        assert "保护值" in record.message


def test_daily_task_skips_when_coins_already_donated(
    daily_db_factory: sessionmaker[Session],
) -> None:
    account_id, _user_id = make_daily_account(daily_db_factory)
    client = FakeDailyClient()
    client.today_exp = 20  # 2 coins already donated today
    service = DailyTaskService(client)  # type: ignore[arg-type]
    with daily_db_factory() as db:
        account = db.get(BiliAccount, account_id)
        assert account is not None
        profile = db.scalar(
            select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account_id)
        )
        assert profile is not None
        profile.share_enabled = False
        db.commit()
        outcome = asyncio.run(service.run(db, account))

        record = db.get(DailyTaskRecord, outcome.record_id)
        assert record is not None
        assert record.coins_donated == 0
        assert record.status == "partial"
        assert "无需再投" in record.message
    assert not any(call.startswith("add:") for call in client.calls)


def test_client_add_coin_and_share_video_build_expected_requests() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "message": "0", "data": None})

    async def exercise() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            bili = BilibiliClient(http_client)
            donated = await bili.add_coin(
                "SESSDATA=session; bili_jct=csrf-token",
                "csrf-token",
                "1001",
                "BV1abc",
                select_like=True,
            )
            assert donated.success is True
            shared = await bili.share_video(
                "SESSDATA=session; bili_jct=csrf-token; buvid3=buvid", "csrf-token", "1001"
            )
            assert shared.success is True

    asyncio.run(exercise())

    add_request, share_request = captured
    assert add_request.url.path == "/x/web-interface/coin/add"
    assert add_request.url.host == "api.bilibili.com"
    assert add_request.headers["referer"] == "https://www.bilibili.com/video/BV1abc/"
    form = dict(httpx.QueryParams(add_request.content.decode("utf-8")).multi_items())
    assert form["aid"] == "1001"
    assert form["csrf"] == "csrf-token"
    assert form["select_like"] == "1"
    assert form["cross_domain"] == "true"

    assert share_request.url.path == "/x/web-interface/share/add"
    share_form = dict(httpx.QueryParams(share_request.content.decode("utf-8")).multi_items())
    assert share_form["aid"] == "1001"
    assert share_form["csrf"] == "csrf-token"


def test_wbi_signing_matches_bilibilitoolpro_reference_vector() -> None:
    # Vector taken from BiliBiliToolPro's WbiServiceTest.EncWbi_InputParams_GetCorrectWbiResult.
    wts, w_rid = _sign_wbi(
        {"foo": "114", "bar": "514", "baz": "1919810"},
        "653657f524a547ac981ded72ea172057",
        "6e4909c702f846728e64f6007736a338",
        wts=1684746387,
    )
    assert wts == 1684746387
    assert w_rid == "d3cbd2a2316089117134038bf4caf442"


def test_wbi_signing_is_deterministic_and_filters_special_characters() -> None:
    img_key = "653657f524a547ac981ded72ea172057"
    sub_key = "6e4909c702f846728e64f6007736a338"
    first_wts, first = _sign_wbi(
        {"mid": "123", "ps": "30", "page": "1"}, img_key, sub_key, wts=100
    )
    second_wts, second = _sign_wbi(
        {"mid": "123", "ps": "30", "page": "1"}, img_key, sub_key, wts=100
    )
    assert (first_wts, first) == (second_wts, second)

    _wts, with_mark = _sign_wbi(
        {"title": "a'b(c)d*e"}, img_key, sub_key, wts=100
    )
    _wts, without_mark = _sign_wbi(
        {"title": "abcde"}, img_key, sub_key, wts=100
    )
    assert with_mark == without_mark


def test_daily_task_profile_api_syncs_schedule_job(
    daily_db_factory: sessionmaker[Session],
) -> None:
    app = create_app()

    def override_db():
        with daily_db_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        created = client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": "correct-horse-42"},
        )
        assert created.status_code == 201
        client.headers.update(
            {
                "Origin": "http://testserver",
                "X-CSRF-Token": client.cookies["csrf_token"],
            }
        )
        with daily_db_factory() as db:
            user = db.scalar(select(User).where(User.username == "owner"))
            assert user is not None
            account = BiliAccount(
                user_id=user.id,
                bili_uid="123456",
                encrypted_cookie=get_credential_cipher().encrypt(
                    "SESSDATA=x; bili_jct=y; buvid3=z"
                ),
            )
            db.add(account)
            db.commit()
            account_id = account.id

        fetched = client.get(f"/api/bili/accounts/{account_id}/daily-task")
        assert fetched.status_code == 200
        assert fetched.json()["enabled"] is False
        assert fetched.json()["target_coins"] == 2

        updated = client.put(
            f"/api/bili/accounts/{account_id}/daily-task",
            json={
                "enabled": True,
                "target_coins": 2,
                "protected_coins": 50,
                "select_like": False,
                "skip_when_lv6": True,
                "share_enabled": True,
                "watch_enabled": False,
                "support_up_ids": [220893216],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is True
        assert updated.json()["support_up_ids"] == [220893216]

        with daily_db_factory() as db:
            job = db.scalar(
                select(ScheduleJob).where(
                    ScheduleJob.bili_account_id == account_id,
                    ScheduleJob.kind == JobKind.DAILY_TASK,
                )
            )
            assert job is not None
            assert job.enabled is True
            assert job.trigger_type == "cron"
            assert job.trigger_config == {"expression": "30 1 * * *"}
            profile = db.scalar(
                select(DailyTaskProfile).where(
                    DailyTaskProfile.bili_account_id == account_id
                )
            )
            assert profile is not None and profile.target_coins == 2

        records = client.get(f"/api/bili/accounts/{account_id}/daily-task-records")
        assert records.status_code == 200
        assert records.json() == []

        # Disabling the profile pauses the cron job.
        disabled = client.put(
            f"/api/bili/accounts/{account_id}/daily-task",
            json={
                "enabled": False,
                "target_coins": 2,
                "protected_coins": 50,
                "select_like": False,
                "skip_when_lv6": True,
                "share_enabled": True,
                "watch_enabled": False,
                "support_up_ids": [],
            },
        )
        assert disabled.status_code == 200
        with daily_db_factory() as db:
            job = db.scalar(
                select(ScheduleJob).where(
                    ScheduleJob.bili_account_id == account_id,
                    ScheduleJob.kind == JobKind.DAILY_TASK,
                )
            )
            assert job is not None and job.enabled is False
        # Records API is tenant-scoped and rejects unknown accounts.
        missing = client.get("/api/bili/accounts/does-not-exist/daily-task")
        assert missing.status_code == 404
