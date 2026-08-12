from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.auth import CurrentUser, DbSession
from app.bilibili.client import (
    BilibiliApiError,
    BilibiliAuthenticationError,
    BilibiliClient,
    get_bilibili_client,
)
from app.models import BiliAccount, CouponClaim
from app.services.coupon import CouponClaimService

router = APIRouter(prefix="/api/bili/accounts", tags=["coupons"])
BiliClientDep = Annotated[BilibiliClient, Depends(get_bilibili_client)]


class CouponView(BaseModel):
    id: str
    claim_month: str
    status: str
    message: str
    checked_at: datetime

    model_config = {"from_attributes": True}


@router.get("/{account_id}/coupon-claims", response_model=list[CouponView])
def list_coupon_claims(account_id: str, user: CurrentUser, db: DbSession) -> list[CouponClaim]:
    return list(
        db.scalars(
            select(CouponClaim)
            .where(
                CouponClaim.bili_account_id == account_id,
                CouponClaim.user_id == user.id,
            )
            .order_by(CouponClaim.checked_at.desc())
        ).all()
    )


@router.post("/{account_id}/coupon-claim", response_model=CouponView)
async def claim_coupon(
    account_id: str,
    user: CurrentUser,
    db: DbSession,
    client: BiliClientDep,
) -> CouponClaim:
    account = db.scalar(
        select(BiliAccount).where(
            BiliAccount.id == account_id,
            BiliAccount.user_id == user.id,
        )
    )
    if account is None:
        await client.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bilibili account not found")
    try:
        outcome = await CouponClaimService(client).claim(db, account)
    except BilibiliAuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bilibili login expired") from exc
    except (BilibiliApiError, OSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "coupon claim failed") from exc
    finally:
        await client.close()
    claim = db.get(CouponClaim, outcome.claim_id)
    if claim is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "coupon result not saved")
    return claim
