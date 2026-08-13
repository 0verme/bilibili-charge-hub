import csv
import hashlib
import hmac
import io
import secrets
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.auth import CurrentUser, DbSession
from app.models import BiliAccount, ChargeRecord, DashboardShare, JobRun
from app.security import hash_password, hash_session_token, verify_password
from app.settings import get_settings

router = APIRouter(tags=["dashboard"])


def local_period_boundaries(now: datetime | None = None) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(get_settings().app_timezone)
    local_now = (now or datetime.now(UTC)).astimezone(timezone)
    today = datetime.combine(local_now.date(), time.min, tzinfo=timezone).astimezone(UTC)
    month = datetime.combine(
        local_now.date().replace(day=1), time.min, tzinfo=timezone
    ).astimezone(UTC)
    return today, month


class DashboardFilters(BaseModel):
    account_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    search: str | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


class ShareInput(BaseModel):
    expires_hours: int = Field(default=24, ge=1, le=24 * 30)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    mask_names: bool = True
    mask_uids: bool = True


def charge_query(user_id: str, filters: DashboardFilters):
    query = select(ChargeRecord).where(ChargeRecord.user_id == user_id)
    if filters.account_id:
        query = query.where(ChargeRecord.bili_account_id == filters.account_id)
    if filters.start:
        query = query.where(ChargeRecord.charged_at >= filters.start)
    if filters.end:
        query = query.where(ChargeRecord.charged_at <= filters.end)
    if filters.search:
        pattern = f"%{filters.search}%"
        query = query.where(
            ChargeRecord.supporter_name.ilike(pattern) | ChargeRecord.supporter_uid.ilike(pattern)
        )
    if filters.min_amount is not None:
        query = query.where(ChargeRecord.amount >= filters.min_amount)
    if filters.max_amount is not None:
        query = query.where(ChargeRecord.amount <= filters.max_amount)
    return query


def mask(value: str) -> str:
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * min(4, len(value) - 2) + value[-1]


def dashboard_payload(
    db: DbSession,
    user_id: str,
    filters: DashboardFilters,
    page: int = 1,
    page_size: int = 20,
    mask_names: bool = False,
    mask_uids: bool = False,
) -> dict:
    base = charge_query(user_id, filters).subquery()
    today, month = local_period_boundaries()
    totals = db.execute(
        select(
            func.coalesce(func.sum(base.c.amount), 0),
            func.coalesce(func.sum(base.c.brokerage), 0),
            func.count(func.distinct(base.c.supporter_uid)),
        )
    ).one()
    today_amount = db.scalar(
        select(func.coalesce(func.sum(base.c.amount), 0)).where(base.c.charged_at >= today)
    )
    month_amount = db.scalar(
        select(func.coalesce(func.sum(base.c.amount), 0)).where(base.c.charged_at >= month)
    )
    total_count = db.scalar(select(func.count()).select_from(base)) or 0
    records = list(
        db.scalars(
            charge_query(user_id, filters)
            .order_by(ChargeRecord.charged_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    trend = db.execute(
        select(func.date(base.c.charged_at), func.sum(base.c.amount))
        .group_by(func.date(base.c.charged_at))
        .order_by(func.date(base.c.charged_at))
    ).all()
    top = db.execute(
        select(base.c.supporter_uid, base.c.supporter_name, func.sum(base.c.amount).label("amount"))
        .group_by(base.c.supporter_uid, base.c.supporter_name)
        .order_by(func.sum(base.c.amount).desc())
        .limit(10)
    ).all()
    accounts = list(db.scalars(select(BiliAccount).where(BiliAccount.user_id == user_id)))
    latest_run = db.scalar(
        select(JobRun).where(JobRun.user_id == user_id).order_by(JobRun.started_at.desc()).limit(1)
    )
    return {
        "summary": {
            "today_amount": str(today_amount),
            "month_amount": str(month_amount),
            "total_amount": str(totals[0]),
            "brokerage": str(totals[1]),
            "platform_difference": str(totals[0] - totals[1]),
            "supporters": totals[2],
        },
        "trend": [{"date": str(day), "amount": str(amount)} for day, amount in trend],
        "top_supporters": [
            {
                "uid": mask(uid) if mask_uids else uid,
                "name": mask(name) if mask_names else name,
                "amount": str(amount),
            }
            for uid, name, amount in top
        ],
        "records": [
            {
                "id": item.id,
                "uid": mask(item.supporter_uid) if mask_uids else item.supporter_uid,
                "name": mask(item.supporter_name) if mask_names else item.supporter_name,
                "amount": str(item.amount),
                "brokerage": str(item.brokerage),
                "charged_at": item.charged_at.isoformat(),
            }
            for item in records
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total_count},
        "accounts": [
            {
                "id": account.id,
                "uid": account.bili_uid,
                "name": account.display_name,
                "status": account.status,
            }
            for account in accounts
        ],
        "latest_run": (
            {"status": latest_run.status, "started_at": latest_run.started_at.isoformat()}
            if latest_run
            else None
        ),
    }


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="dashboard.html", context={"shared": False}
    )


@router.get("/api/dashboard")
def dashboard_api(
    user: CurrentUser,
    db: DbSession,
    account_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    filters = DashboardFilters(
        account_id=account_id,
        start=start,
        end=end,
        search=search,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    return dashboard_payload(db, user.id, filters, page, page_size)


@router.get("/api/dashboard/export.csv")
def export_csv(
    user: CurrentUser,
    db: DbSession,
    account_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
) -> StreamingResponse:
    filters = DashboardFilters(
        account_id=account_id, start=start, end=end, search=search,
        min_amount=min_amount, max_amount=max_amount,
    )

    def generate():
        stream = io.StringIO()
        stream.write("\ufeff")
        writer = csv.writer(stream)
        writer.writerow(["UID", "昵称", "充电金额", "实际到账", "充电时间", "备注"])
        yield stream.getvalue()
        query = charge_query(user.id, filters).order_by(ChargeRecord.charged_at.desc())
        for item in db.scalars(query):
            stream.seek(0)
            stream.truncate(0)
            writer.writerow([item.supporter_uid, item.supporter_name, item.amount,
                             item.brokerage, item.charged_at.isoformat(), item.remark])
            yield stream.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=charge-records.csv"},
    )


@router.post("/api/dashboard/shares", status_code=status.HTTP_201_CREATED)
def create_share(payload: ShareInput, user: CurrentUser, db: DbSession) -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    share = DashboardShare(
        user_id=user.id,
        token_hash=hash_session_token(token),
        password_hash=hash_password(payload.password) if payload.password else None,
        expires_at=datetime.now(UTC) + timedelta(hours=payload.expires_hours),
        mask_names=payload.mask_names,
        mask_uids=payload.mask_uids,
    )
    db.add(share)
    db.commit()
    return {"token": token, "path": f"/share/{token}"}


class ShareView(BaseModel):
    id: str
    expires_at: datetime
    mask_names: bool
    mask_uids: bool
    password_protected: bool


@router.get("/api/dashboard/shares", response_model=list[ShareView])
def list_shares(user: CurrentUser, db: DbSession) -> list[ShareView]:
    shares = db.scalars(
        select(DashboardShare).where(DashboardShare.user_id == user.id)
        .order_by(DashboardShare.created_at.desc())
    )
    return [ShareView(id=item.id, expires_at=item.expires_at, mask_names=item.mask_names,
                      mask_uids=item.mask_uids,
                      password_protected=item.password_hash is not None) for item in shares]


@router.delete("/api/dashboard/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_share(share_id: str, user: CurrentUser, db: DbSession) -> None:
    share = db.scalar(select(DashboardShare).where(
        DashboardShare.id == share_id, DashboardShare.user_id == user.id
    ))
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "share not found")
    db.delete(share)
    db.commit()


def get_share(db: DbSession, token: str) -> DashboardShare:
    share = db.scalar(
        select(DashboardShare).where(DashboardShare.token_hash == hash_session_token(token))
    )
    if share is None or share.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "share not found")
    return share


def share_cookie_name(token: str) -> str:
    return "share_access_" + hash_session_token(token)[:12]


def share_access_signature(token: str) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    return hmac.new(key, f"share|{token}".encode(), hashlib.sha256).hexdigest()


class ShareUnlock(BaseModel):
    password: str = Field(min_length=1, max_length=128)


@router.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
def share_page(token: str, request: Request, db: DbSession) -> HTMLResponse:
    share = get_share(db, token)
    return request.app.state.templates.TemplateResponse(
        request=request, name="share.html",
        context={"token": token, "password_required": share.password_hash is not None},
    )


@router.post("/api/share/{token}/unlock", status_code=status.HTTP_204_NO_CONTENT)
def unlock_share(token: str, payload: ShareUnlock, response: Response, db: DbSession) -> None:
    share = get_share(db, token)
    if share.password_hash and not verify_password(payload.password, share.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect share password")
    response.set_cookie(share_cookie_name(token), share_access_signature(token), max_age=3600,
                        httponly=True, secure=get_settings().app_env == "production",
                        samesite="strict")


@router.get("/api/share/{token}")
def shared_dashboard(token: str, db: DbSession, request: Request) -> dict:
    share = get_share(db, token)
    if share.password_hash:
        supplied = request.cookies.get(share_cookie_name(token), "")
        if not hmac.compare_digest(supplied, share_access_signature(token)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "share password required")
    return dashboard_payload(
        db,
        share.user_id,
        DashboardFilters(),
        mask_names=share.mask_names,
        mask_uids=share.mask_uids,
    )
