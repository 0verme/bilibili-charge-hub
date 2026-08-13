import csv
import hashlib
import hmac
import io
import secrets
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.auth import CurrentUser, DbSession
from app.errors import raise_api_error
from app.models import BiliAccount, ChargeRecord, DashboardShare, JobRun
from app.security import hash_password, hash_session_token, verify_password
from app.settings import get_settings

router = APIRouter(tags=["dashboard"])
SHARE_ACCESS_TTL_SECONDS = 60 * 60


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def filter_datetime_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(get_settings().app_timezone))
    return value.astimezone(UTC)


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

    @field_validator("start", "end")
    @classmethod
    def normalize_filter_datetime(cls, value: datetime | None) -> datetime | None:
        return filter_datetime_to_utc(value) if value is not None else None


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
    include_private_metadata: bool = True,
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
    timezone = ZoneInfo(get_settings().app_timezone)
    trend_totals: dict[str, Decimal] = {}
    for charged_at, amount in db.execute(select(base.c.charged_at, base.c.amount)):
        local_day = as_utc(charged_at).astimezone(timezone).date().isoformat()
        trend_totals[local_day] = trend_totals.get(local_day, Decimal(0)) + amount
    top = db.execute(
        select(base.c.supporter_uid, base.c.supporter_name, func.sum(base.c.amount).label("amount"))
        .group_by(base.c.supporter_uid, base.c.supporter_name)
        .order_by(func.sum(base.c.amount).desc())
        .limit(10)
    ).all()
    payload = {
        "summary": {
            "today_amount": str(today_amount),
            "month_amount": str(month_amount),
            "total_amount": str(totals[0]),
            "brokerage": str(totals[1]),
            "platform_difference": str(totals[0] - totals[1]),
            "supporters": totals[2],
        },
        "trend": [
            {"date": day, "amount": str(amount)}
            for day, amount in sorted(trend_totals.items())
        ],
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
                "charged_at": as_utc(item.charged_at).isoformat(),
            }
            for item in records
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total_count},
        "timezone": get_settings().app_timezone,
    }
    if not include_private_metadata:
        return payload
    accounts = list(db.scalars(select(BiliAccount).where(BiliAccount.user_id == user_id)))
    latest_run = db.scalar(
        select(JobRun).where(JobRun.user_id == user_id).order_by(JobRun.started_at.desc()).limit(1)
    )
    payload.update(
        {
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
                {
                    "status": latest_run.status,
                    "started_at": as_utc(latest_run.started_at).isoformat(),
                }
                if latest_run
                else None
            ),
        }
    )
    return payload


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

    def safe_text(value: object) -> str:
        text = "" if value is None else str(value)
        candidate = text.lstrip(" \t\r\n")
        if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
            return "'" + text
        return text

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
            writer.writerow(
                [
                    safe_text(item.supporter_uid),
                    safe_text(item.supporter_name),
                    item.amount,
                    item.brokerage,
                    as_utc(item.charged_at)
                    .astimezone(ZoneInfo(get_settings().app_timezone))
                    .isoformat(),
                    safe_text(item.remark),
                ]
            )
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
    return [
        ShareView(
            id=item.id,
            expires_at=as_utc(item.expires_at),
            mask_names=item.mask_names,
            mask_uids=item.mask_uids,
            password_protected=item.password_hash is not None,
        )
        for item in shares
    ]


@router.delete("/api/dashboard/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_share(share_id: str, user: CurrentUser, db: DbSession) -> None:
    share = db.scalar(select(DashboardShare).where(
        DashboardShare.id == share_id, DashboardShare.user_id == user.id
    ))
    if share is None:
        raise_api_error(status.HTTP_404_NOT_FOUND, "share_not_found", "share not found")
    db.delete(share)
    db.commit()


def get_share(db: DbSession, token: str) -> DashboardShare:
    share = db.scalar(
        select(DashboardShare).where(DashboardShare.token_hash == hash_session_token(token))
    )
    if share is None or as_utc(share.expires_at) <= datetime.now(UTC):
        raise_api_error(status.HTTP_404_NOT_FOUND, "share_not_found", "share not found")
    return share


def share_cookie_name(token: str) -> str:
    return "share_access_" + hash_session_token(token)[:12]


def share_access_signature(token: str, issued_at: int, expires_at: int) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    value = f"share|{token}|{issued_at}|{expires_at}"
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def issue_share_access(token: str, share: DashboardShare, now: datetime | None = None) -> str:
    issued_at = int(as_utc(now or datetime.now(UTC)).timestamp())
    expires_at = min(
        issued_at + SHARE_ACCESS_TTL_SECONDS,
        int(as_utc(share.expires_at).timestamp()),
    )
    signature = share_access_signature(token, issued_at, expires_at)
    return f"{issued_at}.{expires_at}.{signature}"


def validate_share_access(
    token: str,
    share: DashboardShare,
    supplied: str,
    now: datetime | None = None,
) -> bool:
    try:
        issued_text, expires_text, signature = supplied.split(".", 2)
        issued_at, expires_at = int(issued_text), int(expires_text)
    except (TypeError, ValueError):
        return False
    current = int(as_utc(now or datetime.now(UTC)).timestamp())
    share_expires_at = int(as_utc(share.expires_at).timestamp())
    if (
        issued_at > current + 60
        or expires_at <= current
        or expires_at <= issued_at
        or expires_at - issued_at > SHARE_ACCESS_TTL_SECONDS
        or expires_at > share_expires_at
    ):
        return False
    expected = share_access_signature(token, issued_at, expires_at)
    return hmac.compare_digest(signature, expected)


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
        raise_api_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_share_password",
            "incorrect share password",
        )
    access = issue_share_access(token, share)
    _issued_at, expires_at, _signature = access.split(".", 2)
    max_age = max(1, int(expires_at) - int(datetime.now(UTC).timestamp()))
    response.set_cookie(
        share_cookie_name(token),
        access,
        max_age=max_age,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="strict",
        path=f"/api/share/{token}",
    )


@router.get("/api/share/{token}")
def shared_dashboard(token: str, db: DbSession, request: Request) -> dict:
    share = get_share(db, token)
    if share.password_hash:
        supplied = request.cookies.get(share_cookie_name(token), "")
        if not validate_share_access(token, share, supplied):
            raise_api_error(
                status.HTTP_401_UNAUTHORIZED,
                "share_unlock_required",
                "share password required",
            )
    return dashboard_payload(
        db,
        share.user_id,
        DashboardFilters(),
        mask_names=share.mask_names,
        mask_uids=share.mask_uids,
        include_private_metadata=False,
    )
