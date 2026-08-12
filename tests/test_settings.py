import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(app_timezone="Mars/Olympus_Mons")


def test_production_requires_non_default_secrets() -> None:
    settings = Settings(app_env="production")

    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"):
        settings.validate_runtime_secrets()
