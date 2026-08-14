import asyncio
import hashlib
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
CHARGE_RECORDS_URL = "https://pay.bilibili.com/bk/brokerage/listForCustomerRechargeRecord"
COUPON_CLAIM_URL = "https://api.bilibili.com/x/vip/privilege/receive"
DAILY_TASK_REWARD_URL = "https://api.bilibili.com/x/member/web/exp/reward"
COIN_TODAY_EXP_URL = "https://api.bilibili.com/x/web-interface/coin/today/exp"
ADD_COIN_URL = "https://api.bilibili.com/x/web-interface/coin/add"
SHARE_VIDEO_URL = "https://api.bilibili.com/x/web-interface/share/add"
ARCHIVE_COINS_URL = "https://api.bilibili.com/x/web-interface/archive/coins"
COIN_BALANCE_URL = "https://api.bilibili.com/site/getCoin"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
FOLLOWINGS_URL = "https://api.bilibili.com/x/relation/followings"
RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
SPACE_ARC_SEARCH_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
UPLOAD_HEARTBEAT_URL = "https://api.bilibili.com/x/click-interface/web/heartbeat"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "User-Agent": "Mozilla/5.0 BilibiliChargeHub/0.1",
}
COOKIE_KEYS = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "b_nut",
}


class BilibiliApiError(RuntimeError):
    pass


class BilibiliRateLimited(BilibiliApiError):
    pass


class BilibiliUpstreamUnavailable(BilibiliApiError):
    pass


class BilibiliSchemaChanged(BilibiliApiError):
    pass


class BilibiliBusinessRejected(BilibiliApiError):
    pass


@dataclass(slots=True)
class QrCode:
    key: str
    url: str


@dataclass(slots=True)
class QrPollResult:
    state: str
    message: str
    cookies: dict[str, str] | None = None
    refresh_token: str | None = None


@dataclass(slots=True)
class ChargePage:
    items: list[dict]
    has_more: bool


class BilibiliAuthenticationError(BilibiliApiError):
    pass


@dataclass(slots=True)
class CouponResult:
    status: str
    code: str
    message: str


@dataclass(slots=True)
class DailyTaskReward:
    login: bool
    watch: bool
    share: bool
    coins_exp: int


@dataclass(slots=True)
class VideoInfo:
    aid: str
    bvid: str
    title: str
    cid: str | None = None
    duration: int | None = None
    copyright: int | None = None


@dataclass(slots=True)
class VideoUp:
    mid: str
    uname: str


@dataclass(slots=True)
class NavInfo:
    mid: str
    uname: str
    level: int
    wbi_img_url: str | None = None
    wbi_sub_url: str | None = None


@dataclass(slots=True)
class CoinOpResult:
    code: int
    message: str
    success: bool
    retriable: bool


class BilibiliClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=20, follow_redirects=True
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(
        self, method: str, url: str, **kwargs: object
    ) -> tuple[httpx.Response, dict]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.request(method, url, **kwargs)
                if len(response.content) > 2_000_000:
                    raise BilibiliSchemaChanged("Bilibili response is unexpectedly large")
                if response.status_code == 429:
                    if attempt == 2:
                        raise BilibiliRateLimited("Bilibili rate limit reached")
                    delay = min(float(response.headers.get("Retry-After", "1")), 10)
                    await asyncio.sleep(delay + random.random() / 2)
                    continue
                if response.status_code >= 500:
                    raise BilibiliUpstreamUnavailable(
                        f"Bilibili upstream returned HTTP {response.status_code}"
                    )
                if response.status_code >= 400:
                    raise BilibiliBusinessRejected(
                        f"Bilibili rejected request with HTTP {response.status_code}"
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise BilibiliSchemaChanged("Bilibili response is not valid JSON") from exc
                if not isinstance(body, dict):
                    raise BilibiliSchemaChanged("Bilibili response is not an object")
                return response, body
            except (httpx.TimeoutException, httpx.NetworkError, BilibiliUpstreamUnavailable) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep((2**attempt) + random.random() / 2)
                    continue
        raise BilibiliUpstreamUnavailable("Bilibili upstream unavailable") from last_error

    async def generate_qr(self) -> QrCode:
        _response, body = await self._request_json("GET", GENERATE_URL)
        if body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise BilibiliApiError("Bilibili QR generation failed")
        data = body["data"]
        if not isinstance(data.get("qrcode_key"), str) or not isinstance(data.get("url"), str):
            raise BilibiliSchemaChanged("Bilibili QR response fields changed")
        return QrCode(key=data["qrcode_key"], url=data["url"])

    async def poll_qr(self, key: str) -> QrPollResult:
        response, body = await self._request_json("GET", POLL_URL, params={"qrcode_key": key})
        if body.get("code") != 0:
            raise BilibiliApiError("Bilibili QR polling failed")
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili QR polling data changed")
        code = data.get("code")
        states = {86101: "pending", 86090: "scanned", 86038: "expired"}
        if code != 0:
            return QrPollResult(state=states.get(code, "failed"), message=data.get("message", ""))

        cookies = self._extract_cookies(data.get("url", ""), response)
        if not {"SESSDATA", "bili_jct", "DedeUserID"} <= cookies.keys():
            raise BilibiliApiError("login succeeded without required cookies")
        return QrPollResult(
            state="completed",
            message="login completed",
            cookies=cookies,
            refresh_token=data.get("refresh_token") or None,
        )

    async def fetch_charge_page(
        self,
        cookie_header: str,
        page: int,
        page_size: int = 50,
    ) -> ChargePage:
        _response, body = await self._request_json(
            "GET",
            CHARGE_RECORDS_URL,
            params={"currentPage": page, "pageSize": page_size, "customerId": "10026"},
            headers={"Cookie": cookie_header, "Referer": "https://pay.bilibili.com/"},
        )
        if body.get("code") in {-101, -111, -400, 61000}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict) or "result" not in data:
            raise BilibiliSchemaChanged("Bilibili charge response fields changed")
        items = data.get("result") or []
        if not isinstance(items, list):
            raise BilibiliSchemaChanged("Bilibili charge records are not a list")
        if any(not isinstance(item, dict) for item in items):
            raise BilibiliSchemaChanged("Bilibili charge record item is not an object")
        total = data.get("total") or data.get("totalCount")
        has_more = len(items) >= page_size
        if isinstance(total, int):
            has_more = page * page_size < total
        return ChargePage(items=items, has_more=has_more)

    async def claim_coupon(self, cookie_header: str, csrf: str) -> CouponResult:
        _response, body = await self._request_json(
            "POST",
            COUPON_CLAIM_URL,
            params={"type": 1, "csrf": csrf},
            headers={"Cookie": cookie_header, "Referer": "https://account.bilibili.com/"},
        )
        code = int(body.get("code", -1))
        message = str(body.get("message") or body.get("msg") or "")[:500]
        if code in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        if code == 0:
            state = "success"
        elif "已领取" in message or "already" in message.lower():
            state = "already_claimed"
        elif any(word in message for word in ("不符合", "非大会员", "不可领取")):
            state = "ineligible"
        else:
            state = "error"
        return CouponResult(status=state, code=str(code), message=message)

    async def get_daily_task_reward(self, cookie_header: str) -> DailyTaskReward:
        """Return today's daily-task completion state (login/watch/share/coin exp)."""
        _response, body = await self._request_json(
            "GET",
            DAILY_TASK_REWARD_URL,
            headers={
                "Cookie": cookie_header,
                "Referer": "https://account.bilibili.com/account/home",
                "Origin": "https://account.bilibili.com",
            },
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili daily task reward response fields changed")
        try:
            coins_exp = int(data.get("coins") or 0)
        except (TypeError, ValueError) as exc:
            raise BilibiliSchemaChanged("Bilibili daily task coins is not an integer") from exc
        return DailyTaskReward(
            login=bool(data.get("login")),
            watch=bool(data.get("watch")),
            share=bool(data.get("share")),
            coins_exp=coins_exp,
        )

    async def get_coin_today_exp(self, cookie_header: str) -> int:
        """Return today's experience gained from donating coins (10 exp per 1 coin)."""
        _response, body = await self._request_json(
            "GET",
            COIN_TODAY_EXP_URL,
            headers={
                "Cookie": cookie_header,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            },
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        try:
            return int(body.get("data") or 0)
        except (TypeError, ValueError) as exc:
            raise BilibiliSchemaChanged("Bilibili coin today exp is not an integer") from exc

    async def add_coin(
        self,
        cookie_header: str,
        csrf: str,
        aid: str,
        bvid: str,
        multiply: int = 1,
        select_like: bool = False,
    ) -> CoinOpResult:
        """Donate one or more coins to a video.

        ``retriable`` is True for business codes Bilibili treats as "keep trying with
        another video" (duplicate/self/interval/limit), False for fatal codes.
        """
        _response, body = await self._request_json(
            "POST",
            ADD_COIN_URL,
            data={
                "aid": aid,
                "multiply": multiply,
                "select_like": 1 if select_like else 0,
                "cross_domain": "true",
                "csrf": csrf,
                "eab_x": "2",
                "ramval": "3",
                "source": "web_normal",
                "ga": "1",
            },
            headers={
                "Cookie": cookie_header,
                "Origin": "https://www.bilibili.com",
                "Referer": f"https://www.bilibili.com/video/{bvid}/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        code = int(body.get("code", -1))
        message = str(body.get("message") or body.get("msg") or "")[:500]
        if code in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        if code == 0:
            return CoinOpResult(code=0, message=message, success=True, retriable=True)
        retriable = code in {-400, 10003, 34002, 34003, 34004, 34005}
        return CoinOpResult(code=code, message=message, success=False, retriable=retriable)

    async def share_video(self, cookie_header: str, csrf: str, aid: str) -> CoinOpResult:
        """Share a video. Requires a ``buvid3`` cookie, otherwise Bilibili returns -403."""
        _response, body = await self._request_json(
            "POST",
            SHARE_VIDEO_URL,
            data={
                "aid": aid,
                "csrf": csrf,
                "eab_x": "1",
                "ramval": str(random.randint(3, 20)),
                "source": "web_normal",
                "ga": "1",
            },
            headers={
                "Cookie": cookie_header,
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        code = int(body.get("code", -1))
        message = str(body.get("message") or body.get("msg") or "")[:500]
        if code in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        return CoinOpResult(code=code, message=message, success=code == 0, retriable=True)

    async def upload_heartbeat(
        self,
        cookie_header: str,
        csrf: str,
        video: VideoInfo,
        uid: str,
        played_time: int = 0,
    ) -> CoinOpResult:
        """Report video playback progress; used to complete the daily watch task."""
        _response, body = await self._request_json(
            "POST",
            UPLOAD_HEARTBEAT_URL,
            params={"aid": video.aid, "played_time": played_time},
            data={
                "aid": video.aid,
                "bvid": video.bvid,
                "cid": video.cid or "",
                "mid": uid,
                "csrf": csrf,
                "played_time": played_time,
                "realtime": played_time,
                "real_played_time": played_time,
                "start_ts": int(time.time()),
                "type": "3",
                "dt": "2",
                "play_type": "3",
            },
            headers={
                "Cookie": cookie_header,
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        code = int(body.get("code", -1))
        message = str(body.get("message") or body.get("msg") or "")[:500]
        if code in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        return CoinOpResult(code=code, message=message, success=code == 0, retriable=True)

    async def get_archive_coins(self, cookie_header: str, aid: str) -> int:
        """Return how many coins the account already donated to ``aid``."""
        _response, body = await self._request_json(
            "GET",
            ARCHIVE_COINS_URL,
            params={"aid": aid},
            headers={"Cookie": cookie_header, "Referer": "https://www.bilibili.com/"},
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili archive coins response fields changed")
        try:
            return int(data.get("multiply") or 0)
        except (TypeError, ValueError) as exc:
            raise BilibiliSchemaChanged(
                "Bilibili archive coins multiply is not an integer"
            ) from exc

    async def get_coin_balance(self, cookie_header: str) -> Decimal:
        """Return the account's current coin balance."""
        _response, body = await self._request_json(
            "GET",
            COIN_BALANCE_URL,
            headers={"Cookie": cookie_header, "Referer": "https://account.bilibili.com/account/coin"},
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili coin balance response fields changed")
        try:
            return Decimal(str(data.get("money") or 0))
        except (TypeError, ValueError) as exc:
            raise BilibiliSchemaChanged("Bilibili coin balance is not a number") from exc

    async def get_nav(self, cookie_header: str) -> NavInfo:
        """Return the logged-in profile plus WBI image/sub keys used for signing."""
        _response, body = await self._request_json(
            "GET",
            NAV_URL,
            headers={"Cookie": cookie_header, "Referer": "https://www.bilibili.com/"},
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili nav response fields changed")
        level_info = data.get("level_info") or {}
        wbi_img = data.get("wbi_img") or {}
        return NavInfo(
            mid=str(data.get("mid") or ""),
            uname=str(data.get("uname") or ""),
            level=int(level_info.get("current_level") or 0) if isinstance(level_info, dict) else 0,
            wbi_img_url=wbi_img.get("img_url") if isinstance(wbi_img, dict) else None,
            wbi_sub_url=wbi_img.get("sub_url") if isinstance(wbi_img, dict) else None,
        )

    async def get_followings(
        self, cookie_header: str, uid: str, page: int = 1, page_size: int = 50
    ) -> list[VideoUp]:
        """Return the account's following list (one page, newest first)."""
        _response, body = await self._request_json(
            "GET",
            FOLLOWINGS_URL,
            params={
                "vmid": uid,
                "pn": page,
                "ps": page_size,
                "order": "desc",
                "order_type": "attention",
            },
            headers={"Cookie": cookie_header, "Referer": "https://space.bilibili.com/"},
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise BilibiliSchemaChanged("Bilibili followings response fields changed")
        ups: list[VideoUp] = []
        for item in data["list"]:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("mid") or "")
            if mid:
                ups.append(VideoUp(mid=mid, uname=str(item.get("uname") or "")))
        return ups

    async def get_ranking(self) -> list[VideoInfo]:
        """Return the anonymous all-region ranking as a video pool."""
        _response, body = await self._request_json(
            "GET",
            RANKING_URL,
            params={"rid": 0, "type": "all"},
            headers={"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"},
        )
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise BilibiliSchemaChanged("Bilibili ranking response fields changed")
        videos: list[VideoInfo] = []
        for item in data["list"]:
            if not isinstance(item, dict):
                continue
            videos.append(
                VideoInfo(
                    aid=str(item.get("aid") or ""),
                    bvid=str(item.get("bvid") or ""),
                    title=str(item.get("title") or ""),
                    cid=str(item.get("cid") or ""),
                    duration=int(item.get("duration") or 0),
                    copyright=int(item.get("copyright") or 0),
                )
            )
        return videos

    async def search_up_videos(
        self, cookie_header: str, mid: str, page_number: int = 1, page_size: int = 30
    ) -> tuple[list[VideoInfo], int]:
        """Return one random page of a UP's videos plus the UP's total video count.

        Requires WBI signing; falls back to an empty result when the signing keys
        are unavailable so callers can degrade to followings/ranking pools.
        """
        wbi_keys = await self._get_wbi_keys(cookie_header)
        if wbi_keys is None:
            return [], 0
        img_key, sub_key = wbi_keys
        params = {
            "mid": mid,
            "ps": str(page_size),
            "pn": str(page_number),
            "tid": "0",
            "order": "pubdate",
            "platform": "web",
            "web_location": "1550101",
        }
        wts, w_rid = _sign_wbi(params, img_key, sub_key)
        _response, body = await self._request_json(
            "GET",
            SPACE_ARC_SEARCH_URL,
            params={**params, "wts": wts, "w_rid": w_rid},
            headers={
                "Cookie": cookie_header,
                "Referer": f"https://space.bilibili.com/{mid}/video",
                "Origin": "https://space.bilibili.com",
            },
        )
        if body.get("code") in {-101, -111}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        if body.get("code") != 0:
            return [], 0
        data = body.get("data")
        if not isinstance(data, dict):
            raise BilibiliSchemaChanged("Bilibili space arc search response fields changed")
        page = data.get("page")
        total = int(page.get("count") or 0) if isinstance(page, dict) else 0
        items = data.get("list")
        vlist = items.get("vlist") if isinstance(items, dict) else None
        videos: list[VideoInfo] = []
        if isinstance(vlist, list):
            for item in vlist:
                if not isinstance(item, dict):
                    continue
                videos.append(
                    VideoInfo(
                        aid=str(item.get("aid") or ""),
                        bvid=str(item.get("bvid") or ""),
                        title=str(item.get("title") or ""),
                        duration=_parse_duration_seconds(item.get("length")),
                    )
                )
        return videos, total

    async def _get_wbi_keys(self, cookie_header: str) -> tuple[str, str] | None:
        """Resolve WBI img/sub keys from the nav response (None when unavailable)."""
        nav = await self.get_nav(cookie_header)
        img_key = _file_stem(nav.wbi_img_url or "")
        sub_key = _file_stem(nav.wbi_sub_url or "")
        if not img_key or not sub_key:
            return None
        return img_key, sub_key

    @staticmethod
    def has_buvid3(cookie_header: str) -> bool:
        """Share requests fail with -403 without a buvid3 cookie."""
        parsed = SimpleCookie()
        parsed.load(cookie_header)
        value = parsed.get("buvid3")
        return value is not None and bool(value.value)

    def _extract_cookies(self, cross_domain_url: str, response: httpx.Response) -> dict[str, str]:
        cookies = {key: value for key, value in parse_qsl(urlsplit(cross_domain_url).query)}
        cookies.update(dict(self._client.cookies))
        cookies.update(dict(response.cookies))
        return {key: str(value) for key, value in cookies.items() if key in COOKIE_KEYS and value}


async def get_bilibili_client() -> BilibiliClient:
    return BilibiliClient()


_WBI_MIXIN_KEY_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def _file_stem(url: str) -> str:
    """Return the filename without extension from a Bilibili image URL."""
    return url.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _parse_duration_seconds(value: object) -> int:
    """Parse Bilibili duration formats (``88``, ``04:05``, ``01:02:03``) into seconds."""
    parts = str(value).split(":")
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return 0
    seconds = 0
    for num in nums:
        seconds = seconds * 60 + num
    return seconds


def _sign_wbi(
    parameters: dict[str, str], img_key: str, sub_key: str, wts: int | None = None
) -> tuple[int, str]:
    """Sign query parameters with the WBI mixin-key algorithm (returns wts, w_rid)."""
    mixin_key = "".join((img_key + sub_key)[index] for index in _WBI_MIXIN_KEY_TABLE)[:32]
    if wts is None:
        wts = int(time.time())
    cleaned = {key: re.sub(r"[!'()*]", "", value) for key, value in parameters.items()}
    ordered = sorted({**cleaned, "wts": str(wts)}.items())
    raw = "&".join(f"{quote(key)}={quote(value)}" for key, value in ordered)
    digest = hashlib.md5((raw + mixin_key).encode()).hexdigest()
    return wts, digest
