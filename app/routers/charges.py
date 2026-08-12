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
from app.models import BiliAccount
from app.services.collection import ChargeCollectionService, CollectionBusyError

router = APIRouter(prefix="/api/bili/accounts", tags=["charges"])
BiliClientDep = Annotated[BilibiliClient, Depends(get_bilibili_client)]


class CollectionResultView(BaseModel):
    run_id: str
    pages: int
    seen: int
    inserted: int


@router.post("/{account_id}/collect", response_model=CollectionResultView)
async def collect_account(
    account_id: str,
    user: CurrentUser,
    db: DbSession,
    client: BiliClientDep,
) -> CollectionResultView:
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
        result = await ChargeCollectionService(client).collect(db, account)
    except CollectionBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except BilibiliAuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bilibili login expired") from exc
    except (BilibiliApiError, OSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Bilibili collection failed") from exc
    finally:
        await client.close()
    return CollectionResultView(
        run_id=result.run_id,
        pages=result.pages,
        seen=result.seen,
        inserted=result.inserted,
    )
