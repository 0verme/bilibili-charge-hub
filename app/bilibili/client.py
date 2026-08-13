import asyncio
import random
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import httpx

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
CHARGE_RECORDS_URL = "https://pay.bilibili.com/bk/brokerage/listForCustomerRechargeRecord"
COUPON_CLAIM_URL = "https://api.bilibili.com/x/vip/privilege/receive"
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

    def _extract_cookies(self, cross_domain_url: str, response: httpx.Response) -> dict[str, str]:
        cookies = {key: value for key, value in parse_qsl(urlsplit(cross_domain_url).query)}
        cookies.update(dict(self._client.cookies))
        cookies.update(dict(response.cookies))
        return {key: str(value) for key, value in cookies.items() if key in COOKIE_KEYS and value}


async def get_bilibili_client() -> BilibiliClient:
    return BilibiliClient()
