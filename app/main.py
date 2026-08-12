from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.routers.auth import router as auth_router
from app.routers.auth import users_router
from app.settings import get_settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_runtime_secrets()
    if settings.database_url.get_secret_value().startswith("sqlite"):
        Path("data").mkdir(exist_ok=True)
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Bilibili Charge Hub",
        description="多用户 Bilibili 充电记录、驾驶舱、定时任务与通知中心",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(auth_router)
    application.include_router(users_router)

    @application.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/system/capabilities", tags=["system"])
    def capabilities() -> dict[str, object]:
        return {
            "implemented": [
                "health_check",
                "secure_configuration",
                "docker_runtime",
                "multi_user_authentication",
                "core_database_schema",
            ],
            "milestone": "M1",
        }

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="home.html")

    return application


app = create_app()
