import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.crypto import get_credential_cipher
from app.database import get_session_factory
from app.logging_config import configure_app_logging
from app.routers.accounts import router as accounts_router
from app.routers.auth import router as auth_router
from app.routers.auth import users_router
from app.routers.charges import router as charges_router
from app.routers.coupons import router as coupons_router
from app.routers.dashboard import router as dashboard_router
from app.routers.jobs import router as jobs_router
from app.routers.jobs import runs_router
from app.routers.notifications import router as notifications_router
from app.services.scheduler import SchedulerManager
from app.settings import get_settings
from app.web_security import SlidingWindowLimiter, browser_security

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_app_logging()
    settings = get_settings()
    settings.validate_runtime_secrets()
    if settings.database_url.get_secret_value().startswith("sqlite"):
        Path("data").mkdir(exist_ok=True)
    scheduler = SchedulerManager(get_session_factory(), settings.app_timezone)
    application.state.scheduler = scheduler
    application.state.templates = templates
    application.state.background_tasks = set()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Bilibili Charge Hub",
        description="多用户 Bilibili 充电记录、驾驶舱、定时任务与通知中心",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.state.rate_limiter = SlidingWindowLimiter()
    application.include_router(accounts_router)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(charges_router)
    application.include_router(coupons_router)
    application.include_router(dashboard_router)
    application.include_router(jobs_router)
    application.include_router(runs_router)
    application.include_router(notifications_router)
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    application.middleware("http")(browser_security)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https:; connect-src 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        logging.getLogger("app.request").info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return response

    @application.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz", tags=["system"])
    def readyz(request: Request):
        checks: dict[str, str] = {}
        try:
            with get_session_factory()() as db:
                db.execute(text("SELECT 1"))
                checks["database"] = "ok"
                version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                checks["migration"] = str(version)
            get_credential_cipher()
            checks["cipher"] = "ok"
            checks["scheduler"] = (
                "ok" if request.app.state.scheduler.scheduler.running else "unavailable"
            )
        except Exception as exc:
            checks["error"] = type(exc).__name__
        healthy = not {"error", "unavailable"} & set(checks.values()) and "error" not in checks
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"status": "ready" if healthy else "not_ready", "checks": checks},
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @application.get("/api/system/capabilities", tags=["system"])
    def capabilities() -> dict[str, object]:
        return {
            "implemented": [
                "health_check",
                "secure_configuration",
                "docker_runtime",
                "multi_user_authentication",
                "core_database_schema",
                "bilibili_qr_login",
                "multi_bilibili_accounts",
                "paginated_charge_collection",
                "idempotent_charge_storage",
                "persistent_scheduling",
                "monthly_coupon_claim",
                "notification_plugins",
                "reliable_notification_delivery",
                "dashboard_and_csv_export",
                "expiring_dashboard_shares",
            ],
            "milestone": "M10-single-instance",
        }

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="home.html")

    @application.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="login.html")

    return application


app = create_app()
