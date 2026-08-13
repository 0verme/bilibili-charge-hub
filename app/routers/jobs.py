from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.auth import CurrentUser, DbSession
from app.models import BiliAccount, JobKind, JobRun, RunStatus, ScheduleJob
from app.services.scheduler import SchedulerManager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
runs_router = APIRouter(prefix="/api/job-runs", tags=["job-runs"])


class JobInput(BaseModel):
    bili_account_id: str | None = None
    kind: JobKind
    interval_seconds: int | None = Field(default=None, ge=60)
    cron: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def exactly_one_trigger(self) -> "JobInput":
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError("provide exactly one of interval_seconds or cron")
        account_kinds = {JobKind.CHARGE_COLLECTION, JobKind.COUPON_CLAIM}
        if self.kind in account_kinds and self.bili_account_id is None:
            raise ValueError("this job kind requires bili_account_id")
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
    interval_seconds: int | None = Field(default=None, ge=60)
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
    if payload.bili_account_id:
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
    stored_job = db.get(ScheduleJob, job_id)
    assert stored_job is not None
    run = JobRun(
        user_id=user.id,
        schedule_job_id=job_id,
        status=RunStatus.QUEUED,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    scheduler.submit_dispatch(job_id, run.id)
    return {"status": "queued", "run_id": run.id}


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: str, user: CurrentUser, db: DbSession, scheduler: SchedulerDep
) -> None:
    job = db.scalar(
        select(ScheduleJob).where(ScheduleJob.id == job_id, ScheduleJob.user_id == user.id)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    scheduler.remove_job(job.id)
    db.delete(job)
    db.commit()


class JobRunView(BaseModel):
    id: str
    schedule_job_id: str | None
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    result: dict
    error: str | None

    model_config = {"from_attributes": True}


@runs_router.get("", response_model=list[JobRunView])
def list_job_runs(user: CurrentUser, db: DbSession, limit: int = 100) -> list[JobRun]:
    return list(
        db.scalars(
            select(JobRun)
            .where(JobRun.user_id == user.id)
            .order_by(JobRun.started_at.desc())
            .limit(min(max(limit, 1), 200))
        )
    )


@runs_router.get("/{run_id}", response_model=JobRunView)
def get_job_run(run_id: str, user: CurrentUser, db: DbSession) -> JobRun:
    run = db.scalar(
        select(JobRun).where(JobRun.id == run_id, JobRun.user_id == user.id)
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job run not found")
    return run
