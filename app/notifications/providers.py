import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


@dataclass(slots=True)
class SendResult:
    success: bool
    detail: str


class NotificationProvider(Protocol):
    async def send(self, message: str, config: dict) -> SendResult: ...


def _is_forbidden_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not address.is_global


def validate_webhook_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("webhook URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not contain user information")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in FORBIDDEN_HOSTS or hostname.endswith(".localhost"):
        raise ValueError("webhook URL targets a forbidden host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("webhook URL targets a non-public address")
    return url


async def ensure_public_dns(url: str) -> None:
    hostname = urlsplit(validate_webhook_url(url)).hostname
    assert hostname is not None
    results = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    addresses = {item[4][0] for item in results}
    if not addresses or any(_is_forbidden_ip(address) for address in addresses):
        raise ValueError("webhook DNS resolves to a non-public address")


class HttpProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(self, method: str, url: str, **kwargs: object) -> SendResult:
        try:
            response = await self.client.request(method, url, timeout=10, **kwargs)
        except httpx.HTTPError as exc:
            return SendResult(False, f"request failed: {type(exc).__name__}")
        return SendResult(response.is_success, f"HTTP {response.status_code}")


class FeishuProvider(HttpProvider):
    async def send(self, message: str, config: dict) -> SendResult:
        url = str(config["webhook_url"])
        await ensure_public_dns(url)
        return await self._request(
            "POST", url, json={"msg_type": "text", "content": {"text": message}}
        )


class TelegramProvider(HttpProvider):
    async def send(self, message: str, config: dict) -> SendResult:
        token = str(config["bot_token"])
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        return await self._request(
            "POST", url, json={"chat_id": str(config["chat_id"]), "text": message}
        )


class ServerChanProvider(HttpProvider):
    async def send(self, message: str, config: dict) -> SendResult:
        send_key = str(config["send_key"])
        url = f"https://sctapi.ftqq.com/{send_key}.send"
        return await self._request(
            "POST", url, data={"title": "Bilibili Charge Hub", "desp": message}
        )


class WebhookProvider(HttpProvider):
    async def send(self, message: str, config: dict) -> SendResult:
        url = str(config["url"])
        await ensure_public_dns(url)
        method = str(config.get("method", "POST")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            raise ValueError("webhook method is not allowed")
        headers = {str(key): str(value) for key, value in (config.get("headers") or {}).items()}
        if FORBIDDEN_HEADERS & {key.lower() for key in headers}:
            raise ValueError("webhook contains a forbidden header")
        template = config.get("json_template") or {"message": "{{message}}"}
        rendered = _render_template(template, message)
        return await self._request(method, url, headers=headers, json=rendered)


def _render_template(value: object, message: str) -> object:
    if isinstance(value, str):
        return value.replace("{{message}}", message)
    if isinstance(value, dict):
        return {str(key): _render_template(item, message) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, message) for item in value]
    return value


def provider_registry(client: httpx.AsyncClient) -> dict[str, NotificationProvider]:
    return {
        "feishu": FeishuProvider(client),
        "telegram": TelegramProvider(client),
        "serverchan": ServerChanProvider(client),
        "webhook": WebhookProvider(client),
    }
