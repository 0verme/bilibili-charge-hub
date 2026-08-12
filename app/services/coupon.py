from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bilibili.client import BilibiliAuthenticationError, BilibiliClient
from app.crypto import get_credential_cipher
from app.models import AccountStatus, BiliAccount, CouponClaim, JobRun, RunStatus
from app.notifications.service import enqueue_event


@dataclass(slots=True)
class CouponClaimOutcome:
    claim_id: str
    status: str
    message: str
    run_id: str


def extract_csrf(cookie_header: str) -> str:
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    csrf = parsed.get("bili_jct")
    if csrf is None or not csrf.value:
        raise BilibiliAuthenticationError("Bilibili account authentication expired")
    return csrf.value


class CouponClaimService:
    def __init__(self, client: BilibiliClient) -> None:
        self.client = client

    async def claim(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None = None,
    ) -> CouponClaimOutcome:
        month = datetime.now(UTC).strftime("%Y-%m")
        existing = db.scalar(
            select(CouponClaim).where(
                CouponClaim.bili_account_id == account.id,
                CouponClaim.claim_month == month,
            )
        )
        run = JobRun(
            user_id=account.user_id,
            schedule_job_id=schedule_job_id,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        started = datetime.now(UTC)
        try:
            if existing and existing.status in {"success", "already_claimed"}:
                run.status = RunStatus.SKIPPED
                run.result = {"status": existing.status, "reason": "monthly result exists"}
                return CouponClaimOutcome(existing.id, existing.status, existing.message, run.id)
            cookie = get_credential_cipher().decrypt(account.encrypted_cookie)
            result = await self.client.claim_coupon(cookie, extract_csrf(cookie))
            claim = existing or CouponClaim(
                user_id=account.user_id,
                bili_account_id=account.id,
                claim_month=month,
                status=result.status,
            )
            claim.status = result.status
            claim.result_code = result.code
            claim.message = result.message
            claim.checked_at = datetime.now(UTC)
            db.add(claim)
            db.flush()
            run.status = RunStatus.SUCCEEDED if result.status != "error" else RunStatus.FAILED
            run.result = {"status": result.status, "claim_id": claim.id}
            if result.status == "error":
                run.error = result.message
            event_type = (
                "coupon_claim_succeeded"
                if result.status in {"success", "already_claimed"}
                else "coupon_claim_failed"
            )
            enqueue_event(
                db,
                account.user_id,
                event_type,
                f"coupon:{account.id}:{month}:{result.status}",
                {"account": account.display_name or account.bili_uid, "status": result.status},
            )
            return CouponClaimOutcome(claim.id, claim.status, claim.message, run.id)
        except BilibiliAuthenticationError:
            account.status = AccountStatus.EXPIRED
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
                "coupon_claim_failed",
                f"coupon:{account.id}:{month}:expired",
                {"account": account.display_name or account.bili_uid, "reason": "login expired"},
            )
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
