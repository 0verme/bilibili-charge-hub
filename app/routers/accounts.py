from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.auth import CurrentUser, DbSession
from app.bilibili.client import BilibiliApiError, BilibiliClient, get_bilibili_client
from app.crypto import get_credential_cipher
from app.models import AccountStatus, BiliAccount, JobKind, QrLoginSession, ScheduleJob

router = APIRouter(prefix="/api/bili", tags=["bilibili"])
BiliClientDep = Annotated[BilibiliClient, Depends(get_bilibili_client)]


class QrSessionView(BaseModel):
    id: str
    qr_url: str | None = None
    status: str
    expires_at: datetime
    account_id: str | None = None


class BiliAccountView(BaseModel):
    id: str
    bili_uid: str
    display_name: str
    status: AccountStatus
    last_checked_at: datetime | None

    model_config = {"from_attributes": True}


def get_tenant_qr(db: DbSession, user_id: str, session_id: str) -> QrLoginSession:
    qr_session = db.scalar(
        select(QrLoginSession).where(
            QrLoginSession.id == session_id,
            QrLoginSession.user_id == user_id,
        )
    )
    if qr_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "QR session not found")
    return qr_session


@router.post("/qr-sessions", response_model=QrSessionView, status_code=status.HTTP_201_CREATED)
async def create_qr_session(
    user: CurrentUser,
    db: DbSession,
    client: BiliClientDep,
) -> QrSessionView:
    try:
        qr = await client.generate_qr()
    except (BilibiliApiError, OSError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Bilibili login service unavailable"
        ) from exc
    finally:
        await client.close()
    qr_session = QrLoginSession(
        user_id=user.id,
        qrcode_key=qr.key,
        qr_url=qr.url,
        expires_at=datetime.now(UTC) + timedelta(seconds=180),
    )
    db.add(qr_session)
    db.commit()
    db.refresh(qr_session)
    return QrSessionView(
        id=qr_session.id,
        qr_url=qr_session.qr_url,
        status=qr_session.status,
        expires_at=qr_session.expires_at,
    )


@router.get("/qr-sessions/{session_id}", response_model=QrSessionView)
async def poll_qr_session(
    session_id: str,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    client: BiliClientDep,
) -> QrSessionView:
    qr_session = get_tenant_qr(db, user.id, session_id)
    expires_at = qr_session.expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        qr_session.status = "expired"
        db.commit()
        return QrSessionView(id=qr_session.id, status="expired", expires_at=qr_session.expires_at)
    if qr_session.status == "completed":
        return QrSessionView(id=qr_session.id, status="completed", expires_at=qr_session.expires_at)

    try:
        result = await client.poll_qr(qr_session.qrcode_key)
    except (BilibiliApiError, OSError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Bilibili login service unavailable"
        ) from exc
    finally:
        await client.close()
    qr_session.status = result.state
    account_id = None
    if result.state == "completed" and result.cookies:
        cipher = get_credential_cipher()
        bili_uid = result.cookies["DedeUserID"]
        account = db.scalar(
            select(BiliAccount).where(
                BiliAccount.user_id == user.id,
                BiliAccount.bili_uid == bili_uid,
            )
        )
        cookie_string = "; ".join(f"{key}={value}" for key, value in result.cookies.items())
        if account is None:
            account = BiliAccount(
                user_id=user.id,
                bili_uid=bili_uid,
                encrypted_cookie=cipher.encrypt(cookie_string),
                encrypted_refresh_token=(
                    cipher.encrypt(result.refresh_token) if result.refresh_token else None
                ),
            )
            db.add(account)
        else:
            account.encrypted_cookie = cipher.encrypt(cookie_string)
            account.encrypted_refresh_token = (
                cipher.encrypt(result.refresh_token) if result.refresh_token else None
            )
            account.status = AccountStatus.ACTIVE
        db.flush()
        existing_jobs = set(
            db.scalars(
                select(ScheduleJob.kind).where(
                    ScheduleJob.user_id == user.id,
                    ScheduleJob.bili_account_id == account.id,
                )
            )
        )
        defaults = []
        if JobKind.CHARGE_COLLECTION not in existing_jobs:
            defaults.append(
                ScheduleJob(
                    user_id=user.id,
                    bili_account_id=account.id,
                    kind=JobKind.CHARGE_COLLECTION,
                    trigger_type="interval",
                    trigger_config={"seconds": 60},
                )
            )
        if JobKind.COUPON_CLAIM not in existing_jobs:
            defaults.append(
                ScheduleJob(
                    user_id=user.id,
                    bili_account_id=account.id,
                    kind=JobKind.COUPON_CLAIM,
                    trigger_type="cron",
                    trigger_config={"expression": "0 1 * * *"},
                )
            )
        db.add_all(defaults)
        db.flush()
        scheduler = request.app.state.scheduler
        for job in defaults:
            scheduler.sync_job(job)
        account_id = account.id
    db.commit()
    return QrSessionView(
        id=qr_session.id,
        status=qr_session.status,
        expires_at=qr_session.expires_at,
        account_id=account_id,
    )


@router.get("/accounts", response_model=list[BiliAccountView])
def list_accounts(user: CurrentUser, db: DbSession) -> list[BiliAccount]:
    return list(
        db.scalars(
            select(BiliAccount)
            .where(BiliAccount.user_id == user.id)
            .order_by(BiliAccount.created_at)
        ).all()
    )


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def unbind_account(account_id: str, user: CurrentUser, db: DbSession) -> None:
    account = db.scalar(
        select(BiliAccount).where(
            BiliAccount.id == account_id,
            BiliAccount.user_id == user.id,
        )
    )
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bilibili account not found")
    db.delete(account)
    db.commit()
