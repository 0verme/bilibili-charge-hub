from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import httpx

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
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

    def _extract_cookies(self, cross_domain_url: str, response: httpx.Response) -> dict[str, str]:
        cookies = {key: value for key, value in parse_qsl(urlsplit(cross_domain_url).query)}
        cookies.update(dict(self._client.cookies))
        cookies.update(dict(response.cookies))
        return {key: str(value) for key, value in cookies.items() if key in COOKIE_KEYS and value}


async def get_bilibili_client() -> BilibiliClient:
    return BilibiliClient()
