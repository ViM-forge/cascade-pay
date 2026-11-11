import math
from typing import Self
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Payment-process settings; provider credentials are scoped separately to A/B.

    Compose passes each mock only its own credential as MOCK_SERVICE_CREDENTIAL.
    Sharing a service credential would let a caller authenticated for A access B.
    """

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    redis_url: SecretStr
    jwt_secret: SecretStr
    mock_a_service_credential: SecretStr
    mock_b_service_credential: SecretStr
    merchant_ids: tuple[UUID, UUID]
    jwt_issuer: str = Field(default="payments-demo", min_length=1)
    jwt_audience: str = Field(default="payments-api", min_length=1)
    jwt_ttl_seconds: int = Field(default=900, gt=0, le=900)
    provider_a_url: str = "http://mock-a:8000"
    provider_b_url: str = "http://mock-b:8000"
    http_timeout_seconds: float = Field(default=2, gt=0)
    lease_seconds: float = Field(default=10, gt=0)
    recovery_interval_seconds: float = Field(default=2, gt=0)
    reconciliation_deadline_seconds: float = Field(default=60, gt=0)
    reconciliation_max_checks: int = Field(default=5, gt=0, le=5)
    safe_retry_seconds: float = Field(default=86400, gt=0, le=86400)

    @field_validator("jwt_secret", "mock_a_service_credential", "mock_b_service_credential")
    @classmethod
    def strong_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 32 or raw.lower().startswith(("replace", "change", "placeholder")):
            raise ValueError("secret must be generated and contain at least 32 characters")
        return value

    @field_validator("database_url", "redis_url")
    @classmethod
    def valid_connection_url(cls, value: SecretStr, info) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            allowed = {"postgresql+asyncpg"} if info.field_name == "database_url" else {"redis"}
            if url.drivername not in allowed or not url.host:
                raise ValueError
            if info.field_name == "database_url" and not url.database:
                raise ValueError
            _ = url.port
        except Exception:
            raise ValueError("invalid connection URL or unsupported driver") from None
        return value

    @field_validator("provider_a_url", "provider_b_url")
    @classmethod
    def provider_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("expected HTTP URL without credentials")
        return value

    @model_validator(mode="after")
    def timing_contract(self) -> Self:
        timings = (
            self.http_timeout_seconds,
            self.lease_seconds,
            self.recovery_interval_seconds,
            self.reconciliation_deadline_seconds,
            self.safe_retry_seconds,
        )
        if not all(math.isfinite(v) for v in timings):
            raise ValueError("timings must be finite")
        if self.http_timeout_seconds * 2 >= self.lease_seconds:
            raise ValueError("two HTTP calls must fit within lease")
        if self.recovery_interval_seconds >= self.lease_seconds:
            raise ValueError("recovery interval must be shorter than lease")
        if self.mock_a_service_credential == self.mock_b_service_credential:
            raise ValueError("provider service credentials must differ")
        if len(set(self.merchant_ids)) != 2:
            raise ValueError("two distinct merchant UUIDs required")
        return self


def load_settings() -> Settings:
    # Explicit local .env loading; process environment takes precedence.
    return Settings(_env_file=".env")
