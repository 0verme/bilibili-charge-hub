import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bilibili.client import (
    BilibiliAuthenticationError,
    BilibiliClient,
)
from app.crypto import get_credential_cipher
from app.models import AccountStatus, BiliAccount, ChargeRecord, JobRun, RunStatus, ScheduleJob
from app.notifications.service import enqueue_event

PAGE_SIZE = 50
MAX_PAGES = 100
BILIBILI_TIMEZONE = ZoneInfo("Asia/Shanghai")
_account_locks: dict[str, asyncio.Lock] = {}


class CollectionBusyError(RuntimeError):
    pass


@dataclass(slots=True)
class CollectionResult:
    run_id: str
    pages: int
    seen: int
    inserted: int


def stable_event_id(account_id: str, item: dict) -> str:
    source_id = item.get("id") or item.get("orderNo") or item.get("tradeNo")
    if source_id:
        raw = f"{account_id}|source|{source_id}"
    else:
        raw = "|".join(
            str(value)
            for value in (
                account_id,
                item.get("mid", item.get("uid", "")),
                item.get("name", item.get("nickname", "")),
                item.get("originalThirdCoin", item.get("amount", "")),
                item.get("ctime", item.get("charge_time", "")),
            )
        )
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_charge_time(value: object) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    text = str(value or "").strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), UTC)
    if not text:
        raise ValueError("charge record has no timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BILIBILI_TIMEZONE)
    return parsed.astimezone(UTC)


def parse_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("charge record contains an invalid amount") from exc


def build_charge_record(account: BiliAccount, item: dict) -> ChargeRecord:
    allowed = {
        "id", "orderNo", "tradeNo", "mid", "uid", "name", "nickname", "avatar",
        "originalThirdCoin", "amount", "brokerage", "remark", "ctime", "charge_time",
    }
    raw_data = {
        "schema_version": 1,
        **{key: str(item[key])[:1000] for key in allowed if key in item},
    }
    return ChargeRecord(
        user_id=account.user_id,
        bili_account_id=account.id,
        event_id=stable_event_id(account.id, item),
        supporter_uid=str(item.get("mid", item.get("uid", ""))),
        supporter_name=str(item.get("name", item.get("nickname", ""))),
        avatar_url=str(item.get("avatar", "")),
        amount=parse_decimal(item.get("originalThirdCoin", item.get("amount", 0))),
        brokerage=parse_decimal(item.get("brokerage", 0)),
        remark=str(item.get("remark", "")),
        charged_at=parse_charge_time(item.get("ctime", item.get("charge_time"))),
        raw_data=raw_data,
    )


class ChargeCollectionService:
    def __init__(self, client: BilibiliClient) -> None:
        self.client = client

    async def collect(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None = None,
        run_id: str | None = None,
    ) -> CollectionResult:
        lock = _account_locks.setdefault(account.id, asyncio.Lock())
        if lock.locked():
            raise CollectionBusyError("collection already running for this account")
        async with lock:
            return await self._collect_locked(db, account, schedule_job_id, run_id)

    async def _collect_locked(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None,
        run_id: str | None,
    ) -> CollectionResult:
        run = db.get(JobRun, run_id) if run_id else None
        if run is None:
            run = JobRun(user_id=account.user_id, schedule_job_id=schedule_job_id)
            db.add(run)
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)
        started = datetime.now(UTC)
        pages = seen = inserted = 0
        try:
            cookie = get_credential_cipher().decrypt(account.encrypted_cookie)
            watermark = account.collection_watermark_at
            newest_seen = watermark
            consecutive_known_pages = 0
            for page_number in range(1, MAX_PAGES + 1):
                page = await self.client.fetch_charge_page(cookie, page_number, PAGE_SIZE)
                pages += 1
                page_inserted = 0
                page_times: list[datetime] = []
                for item in page.items:
                    seen += 1
                    record = build_charge_record(account, item)
                    page_times.append(record.charged_at)
                    if newest_seen is None or record.charged_at > newest_seen:
                        newest_seen = record.charged_at
                    exists = db.scalar(
                        select(ChargeRecord.id).where(
                            ChargeRecord.bili_account_id == account.id,
                            ChargeRecord.event_id == record.event_id,
                        )
                    )
                    if exists:
                        continue
                    try:
                        with db.begin_nested():
                            db.add(record)
                            db.flush()
                            enqueue_event(
                                db,
                                account.user_id,
                                "new_charge",
                                f"charge:{account.id}:{record.event_id}",
                                {
                                    "supporter": record.supporter_name,
                                    "amount": str(record.amount),
                                    "charged_at": record.charged_at.isoformat(),
                                },
                            )
                        inserted += 1
                        page_inserted += 1
                    except IntegrityError:
                        pass
                if page_inserted == 0 and watermark and page_times and max(page_times) <= watermark:
                    consecutive_known_pages += 1
                else:
                    consecutive_known_pages = 0
                if not page.has_more or consecutive_known_pages >= 2:
                    break
            account.last_checked_at = datetime.now(UTC)
            account.collection_watermark_at = newest_seen
            run.status = RunStatus.SUCCEEDED
            run.result = {"pages": pages, "seen": seen, "inserted": inserted}
            return CollectionResult(run.id, pages, seen, inserted)
        except BilibiliAuthenticationError:
            account.status = AccountStatus.EXPIRED
            for job in db.scalars(
                select(ScheduleJob).where(ScheduleJob.bili_account_id == account.id)
            ):
                job.enabled = False
                job.next_run_at = None
            run.status = RunStatus.FAILED
            run.error = "Bilibili account authentication expired"
            enqueue_event(
                db,
                account.user_id,
                "cookie_expired",
                f"cookie:{account.id}",
                {"account": account.display_name or account.bili_uid},
            )
            enqueue_event(
                db,
                account.user_id,
                "collection_failed",
                f"collection:{run.id}",
                {"account": account.display_name or account.bili_uid, "reason": "login expired"},
            )
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)[:1000]
            enqueue_event(
                db,
                account.user_id,
                "collection_failed",
                f"collection:{run.id}",
                {"account": account.display_name or account.bili_uid, "reason": "request failed"},
            )
            raise
        finally:
            finished = datetime.now(UTC)
            run.finished_at = finished
            run.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
            db.commit()
