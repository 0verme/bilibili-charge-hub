from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import httpx

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
CHARGE_RECORDS_URL = "https://pay.bilibili.com/bk/brokerage/listForCustomerRechargeRecord"
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


class BilibiliClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=20, follow_redirects=True
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate_qr(self) -> QrCode:
        response = await self._client.get(GENERATE_URL)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0 or not body.get("data"):
            raise BilibiliApiError("Bilibili QR generation failed")
        return QrCode(key=body["data"]["qrcode_key"], url=body["data"]["url"])

    async def poll_qr(self, key: str) -> QrPollResult:
        response = await self._client.get(POLL_URL, params={"qrcode_key": key})
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise BilibiliApiError("Bilibili QR polling failed")
        data = body.get("data") or {}
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
        response = await self._client.get(
            CHARGE_RECORDS_URL,
            params={"currentPage": page, "pageSize": page_size, "customerId": "10026"},
            headers={"Cookie": cookie_header, "Referer": "https://pay.bilibili.com/"},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") in {-101, -111, -400, 61000}:
            raise BilibiliAuthenticationError("Bilibili account authentication expired")
        data = body.get("data") or {}
        items = data.get("result") or []
        if not isinstance(items, list):
            raise BilibiliApiError("unexpected charge record response")
        total = data.get("total") or data.get("totalCount")
        has_more = len(items) >= page_size
        if isinstance(total, int):
            has_more = page * page_size < total
        return ChargePage(items=items, has_more=has_more)

    def _extract_cookies(self, cross_domain_url: str, response: httpx.Response) -> dict[str, str]:
        cookies = {key: value for key, value in parse_qsl(urlsplit(cross_domain_url).query)}
        cookies.update(dict(self._client.cookies))
        cookies.update(dict(response.cookies))
        return {key: str(value) for key, value in cookies.items() if key in COOKIE_KEYS and value}


async def get_bilibili_client() -> BilibiliClient:
    return BilibiliClient()
