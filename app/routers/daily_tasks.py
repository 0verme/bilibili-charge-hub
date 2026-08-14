"""Per-account daily-task configuration and per-day result records."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth import CurrentUser, DbSession
from app.models import DailyTaskProfile, DailyTaskRecord, JobKind, ScheduleJob
from app.routers.jobs import require_tenant_account
from app.services.scheduler import SchedulerManager

router = APIRouter(prefix="/api/bili/accounts", tags=["daily-tasks"])

DAILY_TASK_CRON = "30 1 * * *"


def scheduler_from_request(request: Request) -> SchedulerManager:
    return request.app.state.scheduler


SchedulerDep = Annotated[SchedulerManager, Depends(scheduler_from_request)]


class DailyTaskProfileInput(BaseModel):
    enabled: bool = False
    target_coins: int = Field(default=2, ge=0, le=5)
    protected_coins: int = Field(default=50, ge=0)
    select_like: bool = False
    skip_when_lv6: bool = True
    share_enabled: bool = True
    watch_enabled: bool = False
    support_up_ids: list[int] = Field(default_factory=list)


class DailyTaskProfileView(BaseModel):
    enabled: bool
    target_coins: int
    protected_coins: int
    select_like: bool
    skip_when_lv6: bool
    share_enabled: bool
    watch_enabled: bool
    support_up_ids: list[int]
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class DailyTaskRecordView(BaseModel):
    id: str
    task_date: str
    status: str
    login_done: bool
    watch_done: bool
    share_done: bool
    coins_donated: int
    target_coins: int
    balance_before: float | None
    balance_after: float | None
    share_video: str
    donated_videos: list
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/{account_id}/daily-task", response_model=DailyTaskProfileView)
def get_daily_task_profile(
    account_id: str, user: CurrentUser, db: DbSession
) -> DailyTaskProfile:
    require_tenant_account(db, user.id, account_id)
    profile = db.scalar(
        select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account_id)
    )
    if profile is None:
        return DailyTaskProfile(
            user_id=user.id,
            bili_account_id=account_id,
            enabled=False,
            target_coins=2,
            protected_coins=50,
            select_like=False,
            skip_when_lv6=True,
            share_enabled=True,
            watch_enabled=False,
            support_up_ids=[],
        )
    return profile


@router.put("/{account_id}/daily-task", response_model=DailyTaskProfileView)
def update_daily_task_profile(
    account_id: str,
    payload: DailyTaskProfileInput,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> DailyTaskProfile:
    require_tenant_account(db, user.id, account_id)
    profile = db.scalar(
        select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account_id)
    )
    if profile is None:
        profile = DailyTaskProfile(user_id=user.id, bili_account_id=account_id)
        db.add(profile)
    for field_name in DailyTaskProfileInput.model_fields:
        setattr(profile, field_name, getattr(payload, field_name))
    db.commit()
    db.refresh(profile)
    _sync_schedule_job(db, profile, scheduler)
    return profile


@router.get("/{account_id}/daily-task-records", response_model=list[DailyTaskRecordView])
def list_daily_task_records(
    account_id: str, user: CurrentUser, db: DbSession, limit: int = 30
) -> list[DailyTaskRecord]:
    require_tenant_account(db, user.id, account_id)
    return list(
        db.scalars(
            select(DailyTaskRecord)
            .where(
                DailyTaskRecord.bili_account_id == account_id,
                DailyTaskRecord.user_id == user.id,
            )
            .order_by(DailyTaskRecord.task_date.desc())
            .limit(min(max(limit, 1), 100))
        ).all()
    )


def _sync_schedule_job(
    db: DbSession,
    profile: DailyTaskProfile,
    scheduler: SchedulerManager,
) -> None:
    """Enable/disable the daily-task cron job to mirror the profile switch."""
    job = db.scalar(
        select(ScheduleJob).where(
            ScheduleJob.bili_account_id == profile.bili_account_id,
            ScheduleJob.kind == JobKind.DAILY_TASK,
        )
    )
    if profile.enabled and job is None:
        job = ScheduleJob(
            user_id=profile.user_id,
            bili_account_id=profile.bili_account_id,
            kind=JobKind.DAILY_TASK,
            trigger_type="cron",
            trigger_config={"expression": DAILY_TASK_CRON},
            enabled=True,
        )
        db.add(job)
        db.flush()
        scheduler.sync_job(job, db)
        return
    if job is None:
        return
    if job.enabled != profile.enabled:
        job.enabled = profile.enabled
        db.commit()
        scheduler.sync_job(job, db)
