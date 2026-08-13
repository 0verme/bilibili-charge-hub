import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse

from app.security import hash_session_token

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_WRITE_PATHS = {"/api/auth/setup", "/api/auth/login"}


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - window_seconds:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True


def request_origin_is_same(request: Request) -> bool:
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return True  # Non-browser API clients do not necessarily send either header.
    origin = urlsplit(supplied)
    expected = request.url
    return origin.scheme == expected.scheme and origin.netloc == expected.netloc


async def browser_security(request: Request, call_next):
    if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/"):
        if not request_origin_is_same(request):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        session_token = request.cookies.get("session_token")
        browser_request = request.headers.get("origin") or request.headers.get("referer")
        if session_token and browser_request and request.url.path not in PUBLIC_WRITE_PATHS:
            supplied = request.headers.get("x-csrf-token")
            cookie = request.cookies.get("csrf_token")
            if not supplied or not cookie or not secrets.compare_digest(supplied, cookie):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

    limited = {
        "/api/auth/setup": (5, 300),
        "/api/auth/login": (10, 300),
        "/api/bili/qr-sessions": (10, 60),
    }.get(request.url.path)
    if request.url.path.startswith("/api/bili/accounts/") and request.url.path.endswith(
        "/collect"
    ):
        limited = (5, 60)
    if limited and request.method == "POST":
        client = request.client.host if request.client else "unknown"
        key = hash_session_token(f"{client}|{request.url.path}")
        if not request.app.state.rate_limiter.allow(key, *limited):
            return JSONResponse(
                {"detail": "too many requests"}, status_code=429, headers={"Retry-After": "60"}
            )
    return await call_next(request)
