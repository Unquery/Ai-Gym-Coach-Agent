"""Explicit environment configuration; dotenv loading belongs to the launcher."""

import os
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy.engine import make_url


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_max_overflow: int = Field(default=2, ge=0, le=20)
    db_connect_timeout: int = Field(default=5, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=15000, ge=1000, le=120000)


def load_database_settings() -> DatabaseSettings:
    return DatabaseSettings.model_validate({
        name: os.environ[name.upper()]
        for name in DatabaseSettings.model_fields if name.upper() in os.environ
    })


def validate_production_database_url(value: str) -> None:
    try:
        url = make_url(value)
        if (
            url.drivername != "postgresql+psycopg" or not url.host or not url.database
            or not url.username or not url.password
            or url.query.get("sslmode") != "verify-full"
        ):
            raise ValueError
    except Exception:
        raise ValueError("Production database URL requires PostgreSQL credentials and sslmode=verify-full") from None


class AppSettings(DatabaseSettings):
    environment: Literal["development", "production"] = "development"
    enable_demo_auth: bool = False
    api_auth_token: SecretStr | None = None
    api_owner_id: UUID | None = None
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    cors_origins: tuple[str, ...] = ()
    max_request_bytes: int = Field(default=16384, ge=4096, le=1048576)
    chat_requests_per_minute: int = Field(default=10, ge=1, le=60)
    api_requests_per_minute: int = Field(default=120, ge=1, le=600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def validate_security(self) -> "AppSettings":
        if not self.allowed_hosts or any(
            not host or any(char in host for char in "/:@ ") for host in self.allowed_hosts
        ):
            raise ValueError("ALLOWED_HOSTS must contain hostnames without ports or schemes")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            # Accessing port also rejects malformed/out-of-range port strings.
            parsed.port
            if (
                parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or "*" in origin
            ):
                raise ValueError("CORS_ORIGINS must contain explicit HTTP origins")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("Production CORS origins require HTTPS")
        if self.environment == "production":
            if self.enable_demo_auth:
                raise ValueError("Demo authentication is forbidden in production")
            token = self.api_auth_token.get_secret_value() if self.api_auth_token else ""
            if (
                len(token) < 32 or token.upper().startswith("REPLACE_")
                or not token.isascii() or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)
            ):
                raise ValueError("Production requires a random API_AUTH_TOKEN of at least 32 characters")
            if self.api_owner_id is None or self.api_owner_id.int in {0, 1}:
                raise ValueError("Production requires a non-demo API_OWNER_ID")
            if any("*" in host for host in self.allowed_hosts):
                raise ValueError("Production ALLOWED_HOSTS cannot contain wildcards")
            if self.log_level == "DEBUG":
                raise ValueError("Production debug logging is forbidden")
        return self


def load_settings() -> AppSettings:
    values = {
        name: os.environ[name.upper() if name != "environment" else "APP_ENV"]
        for name in AppSettings.model_fields
        if (name.upper() if name != "environment" else "APP_ENV") in os.environ
    }
    for name in ("allowed_hosts", "cors_origins"):
        if name in values:
            values[name] = tuple(item.strip() for item in values[name].split(",") if item.strip())
    settings = AppSettings.model_validate(values)
    if settings.environment == "production":
        # Errors must never include a connection string or provider credential.
        validate_production_database_url(os.environ.get("DATABASE_URL", ""))
        provider = urlsplit(os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"))
        if (
            provider.scheme != "https" or not provider.hostname or provider.username or provider.password
            or provider.query or provider.fragment
        ):
            raise ValueError("Production LLM_BASE_URL requires HTTPS without credentials, query or fragment")
        from app.llm.config import load_llm_settings

        try:
            llm = load_llm_settings()
            if llm.api_key.get_secret_value().upper().startswith("REPLACE_") or llm.model.upper().startswith("REPLACE_"):
                raise ValueError
        except ValueError:
            raise ValueError("Production requires configured LLM_MODEL and LLM_API_KEY") from None
    return settings
