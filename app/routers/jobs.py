from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.auth import CurrentUser, DbSession
from app.models import BiliAccount, JobKind, ScheduleJob
from app.services.scheduler import SchedulerManager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobInput(BaseModel):
    bili_account_id: str
    kind: JobKind
    interval_seconds: int | None = Field(default=None, ge=10)
    cron: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def exactly_one_trigger(self) -> "JobInput":
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError("provide exactly one of interval_seconds or cron")
        return self


class JobView(BaseModel):
    id: str
    bili_account_id: str | None
    kind: JobKind
    trigger_type: str
    trigger_config: dict
    enabled: bool
    next_run_at: datetime | None

    model_config = {"from_attributes": True}


class ScheduleUpdate(BaseModel):
    interval_seconds: int | None = Field(default=None, ge=10)
    cron: str | None = None

    @model_validator(mode="after")
    def exactly_one_trigger(self) -> "ScheduleUpdate":
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError("provide exactly one of interval_seconds or cron")
        return self


def scheduler_from_request(request: Request) -> SchedulerManager:
    return request.app.state.scheduler


SchedulerDep = Annotated[SchedulerManager, Depends(scheduler_from_request)]


def require_tenant_account(db: DbSession, user_id: str, account_id: str) -> BiliAccount:
    account = db.scalar(
        select(BiliAccount).where(
            BiliAccount.id == account_id,
            BiliAccount.user_id == user_id,
        )
    )
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bilibili account not found")
    return account


@router.get("", response_model=list[JobView])
def list_jobs(user: CurrentUser, db: DbSession) -> list[ScheduleJob]:
    return list(
        db.scalars(
            select(ScheduleJob)
            .where(ScheduleJob.user_id == user.id)
            .order_by(ScheduleJob.created_at)
        ).all()
    )


@router.post("", response_model=JobView, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobInput,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> ScheduleJob:
    require_tenant_account(db, user.id, payload.bili_account_id)
    trigger_type = "interval" if payload.interval_seconds is not None else "cron"
    trigger_config = (
        {"seconds": payload.interval_seconds}
        if payload.interval_seconds is not None
        else {"expression": payload.cron}
    )
    job = ScheduleJob(
        user_id=user.id,
        bili_account_id=payload.bili_account_id,
        kind=payload.kind,
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        enabled=payload.enabled,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if job.enabled:
        try:
            scheduler.sync_job(job, db)
        except ValueError as exc:
            db.delete(job)
            db.commit()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid trigger") from exc
    return job


@router.patch("/{job_id}/enabled", response_model=JobView)
def set_job_enabled(
    job_id: str,
    enabled: bool,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> ScheduleJob:
    job = db.scalar(
        select(ScheduleJob).where(ScheduleJob.id == job_id, ScheduleJob.user_id == user.id)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    job.enabled = enabled
    db.commit()
    scheduler.sync_job(job, db)
    return job


@router.patch("/{job_id}/schedule", response_model=JobView)
def update_job_schedule(
    job_id: str,
    payload: ScheduleUpdate,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> ScheduleJob:
    job = db.scalar(
        select(ScheduleJob).where(ScheduleJob.id == job_id, ScheduleJob.user_id == user.id)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if payload.interval_seconds is not None:
        job.trigger_type = "interval"
        job.trigger_config = {"seconds": payload.interval_seconds}
    else:
        job.trigger_type = "cron"
        job.trigger_config = {"expression": payload.cron}
    if not job.enabled:
        db.commit()
        return job
    try:
        scheduler.sync_job(job, db)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid trigger") from exc
    return job


@router.post("/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_job_now(
    job_id: str,
    user: CurrentUser,
    db: DbSession,
    scheduler: SchedulerDep,
) -> dict[str, str]:
    job = db.scalar(
        select(ScheduleJob.id).where(ScheduleJob.id == job_id, ScheduleJob.user_id == user.id)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    await scheduler.dispatch(job_id)
    return {"status": "completed"}
