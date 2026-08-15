from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_timezone: str = "Asia/Shanghai"
    database_url: SecretStr = SecretStr("sqlite:///./data/bilibili-charge-hub.sqlite3")
    app_secret_key: SecretStr = SecretStr("development-only-change-me")
    credential_encryption_key: SecretStr | None = None
    collection_interval_seconds: int = Field(default=300, ge=20)
    retention_days: int = Field(default=90, ge=7)
    notification_reconciliation_lookback_hours: int = Field(default=24, ge=1, le=168)
    notification_reconciliation_max_records: int = Field(default=2000, ge=50)

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    def validate_runtime_secrets(self) -> None:
        if self.app_env != "production":
            return
        if self.app_secret_key.get_secret_value() == "development-only-change-me":
            raise RuntimeError("APP_SECRET_KEY must be configured in production")
        if not self.credential_encryption_key:
            raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be configured in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
