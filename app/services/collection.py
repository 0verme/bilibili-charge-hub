import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bilibili.client import (
    BilibiliAuthenticationError,
    BilibiliClient,
)
from app.crypto import get_credential_cipher
from app.models import AccountStatus, BiliAccount, ChargeRecord, JobRun, RunStatus

PAGE_SIZE = 50
MAX_PAGES = 100
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
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def parse_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("charge record contains an invalid amount") from exc


def build_charge_record(account: BiliAccount, item: dict) -> ChargeRecord:
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
        raw_data=json.loads(json.dumps(item, ensure_ascii=False, default=str)),
    )


class ChargeCollectionService:
    def __init__(self, client: BilibiliClient) -> None:
        self.client = client

    async def collect(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None = None,
    ) -> CollectionResult:
        lock = _account_locks.setdefault(account.id, asyncio.Lock())
        if lock.locked():
            raise CollectionBusyError("collection already running for this account")
        async with lock:
            return await self._collect_locked(db, account, schedule_job_id)

    async def _collect_locked(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None,
    ) -> CollectionResult:
        run = JobRun(
            user_id=account.user_id,
            schedule_job_id=schedule_job_id,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        started = datetime.now(UTC)
        pages = seen = inserted = 0
        try:
            cookie = get_credential_cipher().decrypt(account.encrypted_cookie)
            for page_number in range(1, MAX_PAGES + 1):
                page = await self.client.fetch_charge_page(cookie, page_number, PAGE_SIZE)
                pages += 1
                for item in page.items:
                    seen += 1
                    record = build_charge_record(account, item)
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
                        inserted += 1
                    except IntegrityError:
                        pass
                if not page.has_more:
                    break
            account.last_checked_at = datetime.now(UTC)
            run.status = RunStatus.SUCCEEDED
            run.result = {"pages": pages, "seen": seen, "inserted": inserted}
            return CollectionResult(run.id, pages, seen, inserted)
        except BilibiliAuthenticationError:
            account.status = AccountStatus.EXPIRED
            run.status = RunStatus.FAILED
            run.error = "Bilibili account authentication expired"
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)[:1000]
            raise
        finally:
            finished = datetime.now(UTC)
            run.finished_at = finished
            run.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
            db.commit()
