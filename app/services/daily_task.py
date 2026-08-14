"""Opt-in daily tasks: watch/share videos and donate coins for daily experience.

Donating coins consumes an account's coin balance, so this service only acts when
the account's ``DailyTaskProfile.enabled`` is True. Every execution writes a
per-account per-day ``DailyTaskRecord`` and reuses the account expiry flow shared
with coupon claiming.
"""

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bilibili.client import (
    BilibiliAuthenticationError,
    BilibiliClient,
    NavInfo,
    VideoInfo,
)
from app.crypto import get_credential_cipher
from app.models import (
    AccountStatus,
    BiliAccount,
    DailyTaskProfile,
    DailyTaskRecord,
    JobRun,
    RunStatus,
    ScheduleJob,
)
from app.notifications.service import enqueue_event
from app.services.coupon import extract_csrf
from app.settings import get_settings

logger = logging.getLogger(__name__)

MAX_DONATE_ATTEMPTS = 10
MAX_VIDEO_POOL_SIZE = 40
FOLLOWING_UPS_TO_TRY = 5


@dataclass(slots=True)
class DailyTaskOutcome:
    record_id: str
    status: str
    message: str
    run_id: str


def local_task_date(now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo

    return (now or datetime.now(UTC)).astimezone(
        ZoneInfo(get_settings().app_timezone)
    ).strftime("%Y-%m-%d")


class DailyTaskService:
    def __init__(self, client: BilibiliClient) -> None:
        self.client = client

    async def run(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None = None,
        run_id: str | None = None,
    ) -> DailyTaskOutcome:
        task_date = local_task_date()
        profile = self._get_or_create_profile(db, account)
        run = self._start_run(db, account, schedule_job_id, run_id)
        started = datetime.now(UTC)
        try:
            if not profile.enabled:
                return self._skip(run, task_date, "每日任务未启用", db, account, profile)

            existing = db.scalar(
                select(DailyTaskRecord).where(
                    DailyTaskRecord.bili_account_id == account.id,
                    DailyTaskRecord.task_date == task_date,
                )
            )
            if existing and existing.status in {"success", "partial"}:
                return self._skip(
                    run,
                    task_date,
                    "今日任务已完成",
                    db,
                    account,
                    profile,
                    existing=existing,
                )

            cookie = get_credential_cipher().decrypt(account.encrypted_cookie)
            csrf = extract_csrf(cookie)

            reward = await self.client.get_daily_task_reward(cookie)
            nav = await self.client.get_nav(cookie)

            record = DailyTaskRecord(
                user_id=account.user_id,
                bili_account_id=account.id,
                task_date=task_date,
                status="running",
                login_done=reward.login,
                watch_done=reward.watch,
                share_done=reward.share,
                coins_donated=0,
                target_coins=profile.target_coins,
                message="",
            )
            db.add(record)
            db.flush()
            record_id = record.id

            messages: list[str] = []

            if profile.watch_enabled and not record.watch_done:
                ok, _video, msg = await self._watch_video(cookie, csrf, account, profile)
                record.watch_done = ok
                messages.append(msg)

            if profile.share_enabled and not record.share_done:
                ok, _video, msg = await self._share_video(cookie, csrf, account, profile)
                record.share_done = ok
                messages.append(msg)

            donated, videos, msg = await self._donate_coins(
                cookie, csrf, account, profile, nav, record
            )
            record.coins_donated = donated
            record.donated_videos = videos
            messages.append(msg)

            record.login_done = record.login_done or bool(nav.mid)
            record.message = "；".join(m for m in messages if m)
            completed = record.watch_done or record.share_done or donated > 0
            record.status = "success" if completed else "partial"
            run.status = RunStatus.SUCCEEDED
            run.result = {
                "status": record.status,
                "task_date": task_date,
                "coins_donated": donated,
                "watch_done": record.watch_done,
                "share_done": record.share_done,
            }
            event_type = "daily_task_succeeded" if completed else "daily_task_failed"
            enqueue_event(
                db,
                account.user_id,
                event_type,
                f"daily:{account.id}:{task_date}:{record.status}",
                {
                    "account": account.display_name or account.bili_uid,
                    "status": record.status,
                    "coins_donated": donated,
                    "share_done": record.share_done,
                    "watch_done": record.watch_done,
                },
            )
            return DailyTaskOutcome(record_id, record.status, record.message, run.id)
        except BilibiliAuthenticationError:
            account.status = AccountStatus.EXPIRED
            for job in db.scalars(
                select(ScheduleJob).where(ScheduleJob.bili_account_id == account.id)
            ):
                job.enabled = False
                job.next_run_at = None
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
                "daily_task_failed",
                f"daily:{account.id}:{task_date}:expired",
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

    # ------------------------------------------------------------------ private

    def _get_or_create_profile(self, db: Session, account: BiliAccount) -> DailyTaskProfile:
        profile = db.scalar(
            select(DailyTaskProfile).where(DailyTaskProfile.bili_account_id == account.id)
        )
        if profile is None:
            profile = DailyTaskProfile(user_id=account.user_id, bili_account_id=account.id)
            db.add(profile)
            db.flush()
        return profile

    def _start_run(
        self,
        db: Session,
        account: BiliAccount,
        schedule_job_id: str | None,
        run_id: str | None,
    ) -> JobRun:
        run = db.get(JobRun, run_id) if run_id else None
        if run is None:
            run = JobRun(user_id=account.user_id, schedule_job_id=schedule_job_id)
            db.add(run)
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)
        return run

    def _skip(
        self,
        run: JobRun,
        task_date: str,
        reason: str,
        db: Session,
        account: BiliAccount,
        profile: DailyTaskProfile,
        existing: DailyTaskRecord | None = None,
    ) -> DailyTaskOutcome:
        run.status = RunStatus.SKIPPED
        run.result = {"status": "skipped", "reason": reason, "task_date": task_date}
        record = existing
        if record is None:
            record = DailyTaskRecord(
                user_id=account.user_id,
                bili_account_id=account.id,
                task_date=task_date,
                status="skipped",
                target_coins=profile.target_coins,
                message=reason,
            )
            db.add(record)
            db.flush()
        run.finished_at = datetime.now(UTC)
        run.duration_ms = 0
        db.commit()
        return DailyTaskOutcome(record.id, "skipped", reason, run.id)

    async def _watch_video(
        self, cookie: str, csrf: str, account: BiliAccount, profile: DailyTaskProfile
    ) -> tuple[bool, VideoInfo | None, str]:
        """Complete the daily watch task with two heartbeat reports (open + progress)."""
        video = await self._pick_watch_video(cookie)
        if video is None or not video.cid:
            return False, None, "观看：暂无可用视频"
        try:
            opened = await self.client.upload_heartbeat(cookie, csrf, video, account.bili_uid, 0)
            if not opened.success:
                return False, video, f"观看：打开视频失败（{opened.message}）"
            played = random.randint(1, min(15, video.duration or 15))
            finished = await self.client.upload_heartbeat(
                cookie, csrf, video, account.bili_uid, played
            )
            if finished.success:
                return True, video, f"观看：{video.title}（播放 {played}s）"
            return False, video, f"观看：播放上报失败（{finished.message}）"
        except BilibiliAuthenticationError:
            raise
        except Exception as exc:
            return False, video, f"观看：异常（{exc}）"

    async def _share_video(
        self, cookie: str, csrf: str, account: BiliAccount, profile: DailyTaskProfile
    ) -> tuple[bool, VideoInfo | None, str]:
        """Complete the daily share task; requires a buvid3 cookie."""
        if not self.client.has_buvid3(cookie):
            return False, None, "分享：Cookie 缺少 buvid3，无法分享"
        video = await self._next_video(cookie, account, profile, [], set())
        if video is None:
            return False, None, "分享：暂无可用视频"
        try:
            result = await self.client.share_video(cookie, csrf, video.aid)
        except BilibiliAuthenticationError:
            raise
        except Exception as exc:
            return False, video, f"分享：异常（{exc}）"
        if result.success:
            return True, video, f"分享：{video.title}"
        return False, video, f"分享：失败（{result.message}）"

    async def _donate_coins(
        self,
        cookie: str,
        csrf: str,
        account: BiliAccount,
        profile: DailyTaskProfile,
        nav: NavInfo,
        record: DailyTaskRecord,
    ) -> tuple[int, list[dict], str]:
        """Donate up to ``target_coins`` coins with balance and per-video protections."""
        if profile.target_coins <= 0:
            return 0, [], "投币：已配置为跳过"
        if profile.skip_when_lv6 and nav.level >= 6:
            return 0, [], "投币：LV6 已配置跳过"
        try:
            today_exp = await self.client.get_coin_today_exp(cookie)
        except Exception as exc:
            return 0, [], f"投币：查询今日经验失败（{exc}）"
        already = today_exp // 10
        need = profile.target_coins - already
        if need <= 0:
            return 0, [], f"投币：今日已投 {already} 枚，无需再投"
        try:
            balance = await self.client.get_coin_balance(cookie)
        except Exception as exc:
            return 0, [], f"投币：查询余额失败（{exc}）"
        record.balance_before = balance
        if balance <= 0:
            return 0, [], "投币：硬币余额为 0，跳过"
        affordable = int(balance) - profile.protected_coins
        if affordable <= 0:
            return 0, [], f"投币：余额 {balance} 不高于保护值 {profile.protected_coins}，跳过"
        need = min(need, affordable)
        need = max(0, min(need, MAX_DONATE_ATTEMPTS))
        if need <= 0:
            return 0, [], "投币：无可用投币额度"

        pool: list[VideoInfo] = []
        tried: set[str] = set()
        done_videos: list[dict] = []
        success = 0
        attempts = 0
        while success < need and attempts < MAX_DONATE_ATTEMPTS:
            attempts += 1
            video = await self._next_video(cookie, account, profile, pool, tried)
            if video is None:
                break
            tried.add(video.aid)
            try:
                already_on_video = await self.client.get_archive_coins(cookie, video.aid)
            except BilibiliAuthenticationError:
                raise
            except Exception:
                continue
            if already_on_video >= 2:
                continue
            result = await self.client.add_coin(
                cookie, csrf, video.aid, video.bvid, select_like=profile.select_like
            )
            if result.success:
                success += 1
                done_videos.append(
                    {"aid": video.aid, "bvid": video.bvid, "title": video.title}
                )
            elif not result.retriable:
                break
        try:
            record.balance_after = await self.client.get_coin_balance(cookie)
        except Exception:
            record.balance_after = None
        if success >= need:
            message = f"投币：成功 {success} 枚"
        elif success > 0:
            message = f"投币：成功 {success}/{need} 枚"
        else:
            message = "投币：未能投出硬币"
        return success, done_videos, message

    async def _next_video(
        self,
        cookie: str,
        account: BiliAccount,
        profile: DailyTaskProfile,
        pool: list[VideoInfo],
        tried: set[str],
    ) -> VideoInfo | None:
        """Pop one untried video from the pool, refilling it when exhausted."""
        refills = 0
        while True:
            untried = [v for v in pool if v.aid not in tried and v.aid]
            if untried:
                return random.choice(untried)
            if pool:
                pool.clear()
            if refills >= 3:
                return None
            refills += 1
            await self._fill_pool(cookie, account, profile, pool, tried)
            if not pool:
                return None

    async def _fill_pool(
        self,
        cookie: str,
        account: BiliAccount,
        profile: DailyTaskProfile,
        pool: list[VideoInfo],
        tried: set[str],
    ) -> None:
        """Fill the candidate pool: configured UPs, then followings, then ranking."""
        up_ids = list(profile.support_up_ids or [])
        random.shuffle(up_ids)
        for mid in up_ids:
            if len(pool) >= MAX_VIDEO_POOL_SIZE:
                return
            videos, _total = await self._safe_search_up(cookie, mid)
            pool.extend(v for v in videos if v.aid not in tried)
        if not pool:
            followings = await self._safe_followings(cookie, account.bili_uid)
            random.shuffle(followings)
            for up in followings[:FOLLOWING_UPS_TO_TRY]:
                if len(pool) >= MAX_VIDEO_POOL_SIZE:
                    return
                videos, _total = await self._safe_search_up(cookie, up.mid)
                pool.extend(v for v in videos if v.aid not in tried)
        if not pool:
            try:
                pool.extend(v for v in await self.client.get_ranking() if v.aid not in tried)
            except BilibiliAuthenticationError:
                raise
            except Exception:
                logger.warning("ranking fetch failed during daily task", exc_info=True)

    async def _safe_search_up(self, cookie: str, mid: str) -> tuple[list[VideoInfo], int]:
        try:
            return await self.client.search_up_videos(cookie, mid, page_number=1, page_size=30)
        except BilibiliAuthenticationError:
            raise
        except Exception:
            logger.warning("up video search failed", extra={"mid": mid}, exc_info=True)
            return [], 0

    async def _safe_followings(self, cookie: str, uid: str):
        try:
            return await self.client.get_followings(cookie, uid, page_size=50)
        except BilibiliAuthenticationError:
            raise
        except Exception:
            logger.warning("followings fetch failed during daily task", exc_info=True)
            return []

    async def _pick_watch_video(self, cookie: str) -> VideoInfo | None:
        """Watch tasks need a cid, so they draw from the anonymous ranking pool."""
        try:
            ranking = await self.client.get_ranking()
        except BilibiliAuthenticationError:
            raise
        except Exception:
            return None
        candidates = [v for v in ranking if v.cid]
        if not candidates:
            return None
        return random.choice(candidates)
