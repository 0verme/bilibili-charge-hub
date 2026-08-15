import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth import DbSession, SessionToken, get_optional_current_user, has_active_admin
from app.crypto import get_credential_cipher
from app.database import get_session_factory
from app.logging_config import configure_app_logging
from app.readiness import MigrationReadiness, check_migration_readiness, get_code_heads
from app.routers.accounts import router as accounts_router
from app.routers.auth import router as auth_router
from app.routers.auth import users_router
from app.routers.charges import router as charges_router
from app.routers.coupons import router as coupons_router
from app.routers.daily_tasks import router as daily_tasks_router
from app.routers.dashboard import router as dashboard_router
from app.routers.jobs import router as jobs_router
from app.routers.jobs import runs_router
from app.routers.notifications import router as notifications_router
from app.services.scheduler import SchedulerManager
from app.settings import get_settings
from app.web_security import SlidingWindowLimiter, browser_security

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
logger = logging.getLogger(__name__)


def _failed_migration_check(reason: str) -> MigrationReadiness:
    try:
        expected_heads = get_code_heads()
    except Exception:
        expected_heads = ()
    return MigrationReadiness((), expected_heads, reason)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_app_logging()
    settings = get_settings()
    settings.validate_runtime_secrets()
    if settings.database_url.get_secret_value().startswith("sqlite"):
        Path("data").mkdir(exist_ok=True)
    factory = get_session_factory()
    scheduler = SchedulerManager(factory, settings.app_timezone)
    application.state.scheduler = scheduler
    application.state.templates = templates
    try:
        with factory() as db:
            migration = check_migration_readiness(db.connection())
    except Exception:
        logger.exception("database migration readiness inspection failed")
        migration = _failed_migration_check("inspection_failed")
    application.state.startup_migration = migration
    if migration.ready:
        scheduler.start()
    else:
        logger.error(
            "scheduler disabled because database migrations are not current",
            extra={
                "migration_reason": migration.reason,
                "migration_current_heads": migration.current_heads,
                "migration_expected_heads": migration.expected_heads,
            },
        )
    try:
        yield
    finally:
        await scheduler.shutdown_gracefully(timeout_seconds=10)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Bilibili Charge Hub",
        description="多用户 Bilibili 充电记录、驾驶舱、定时任务与通知中心",
        version="0.3.1",
        lifespan=lifespan,
    )
    application.state.rate_limiter = SlidingWindowLimiter()
    application.include_router(accounts_router)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(charges_router)
    application.include_router(coupons_router)
    application.include_router(daily_tasks_router)
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
    def readyz(request: Request) -> JSONResponse:
        checks: dict[str, object] = {}
        migration_ready = False
        try:
            with get_session_factory()() as db:
                db.execute(text("SELECT 1"))
                checks["database"] = "ok"
                migration = check_migration_readiness(db.connection())
                checks["migration"] = migration.as_dict()
                migration_ready = migration.ready
        except Exception:
            logger.exception("readiness database inspection failed")
            checks["database"] = "unavailable"
            checks["migration"] = _failed_migration_check("inspection_failed").as_dict()

        try:
            get_credential_cipher()
            checks["cipher"] = "ok"
        except Exception:
            logger.exception("readiness credential cipher inspection failed")
            checks["cipher"] = "unavailable"

        scheduler_ready = request.app.state.scheduler.scheduler.running
        checks["scheduler"] = "ok" if scheduler_ready else "unavailable"
        healthy = (
            checks["database"] == "ok"
            and migration_ready
            and checks["cipher"] == "ok"
            and scheduler_ready
        )

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
                "opt_in_daily_tasks",
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
    def login_page(
        request: Request,
        db: DbSession,
        session_token: SessionToken = None,
    ) -> Response:
        if get_optional_current_user(db, session_token):
            return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        if not has_active_admin(db):
            return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request=request, name="login.html")

    @application.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page(
        request: Request,
        db: DbSession,
        session_token: SessionToken = None,
    ) -> Response:
        if get_optional_current_user(db, session_token):
            return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        if has_active_admin(db):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request=request, name="setup.html")

    return application


app = create_app()
