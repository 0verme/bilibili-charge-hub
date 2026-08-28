import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse

from app.errors import error_detail
from app.security import hash_session_token

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_WRITE_PATHS = {"/api/auth/setup", "/api/auth/login", "/api/auth/recover"}


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
        return False
    origin = urlsplit(supplied)
    expected = request.url
    return origin.scheme == expected.scheme and origin.netloc == expected.netloc


async def browser_security(request: Request, call_next):
    if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/"):
        session_token = request.cookies.get("session_token")
        supplied_origin = request.headers.get("origin") or request.headers.get("referer")
        session_protected = session_token and request.url.path not in PUBLIC_WRITE_PATHS
        if session_protected:
            if not supplied_origin:
                return JSONResponse(
                    {"detail": error_detail("request_origin_required", "request origin required")},
                    status_code=403,
                )
            if not request_origin_is_same(request):
                return JSONResponse(
                    {
                        "detail": error_detail(
                            "cross_origin_rejected", "cross-origin request rejected"
                        )
                    },
                    status_code=403,
                )
            supplied = request.headers.get("x-csrf-token")
            cookie = request.cookies.get("csrf_token")
            if not supplied or not cookie or not secrets.compare_digest(supplied, cookie):
                return JSONResponse(
                    {"detail": error_detail("csrf_validation_failed", "CSRF validation failed")},
                    status_code=403,
                )
        elif supplied_origin and not request_origin_is_same(request):
            return JSONResponse(
                {"detail": error_detail("cross_origin_rejected", "cross-origin request rejected")},
                status_code=403,
            )

    limited = {
        "/api/auth/setup": (5, 300),
        "/api/auth/login": (10, 300),
        "/api/auth/recover": (5, 300),
        "/api/bili/qr-sessions": (10, 60),
        "/api/notifications/reconcile": (2, 300),
    }.get(request.url.path)
    if request.url.path.startswith("/api/bili/accounts/") and request.url.path.endswith(
        "/collect"
    ):
        limited = (5, 60)
    if request.url.path.startswith("/api/share/") and request.url.path.endswith("/unlock"):
        limited = (5, 300)
    if limited and request.method == "POST":
        client = request.client.host if request.client else "unknown"
        key = hash_session_token(f"{client}|{request.url.path}")
        if not request.app.state.rate_limiter.allow(key, *limited):
            return JSONResponse(
                {"detail": error_detail("rate_limited", "too many requests")},
                status_code=429,
                headers={"Retry-After": str(limited[1])},
            )
    return await call_next(request)
