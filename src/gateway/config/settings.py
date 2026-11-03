"""Environment-backed Pydantic Settings and configuration validation."""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_EXAMPLE_KEK = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
INSECURE_BOOTSTRAP_TOKEN = "ugw_admin_dev_bootstrap_replace_me_in_prod"


class GatewaySettings(BaseSettings):
    """Centralized, strictly validated configuration for Universal AI Gateway."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application Environment
    env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "Universal AI Gateway"
    version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    api_v1_prefix: str = "/api/v1"

    # Security & Encryption
    master_kek_base64: SecretStr = Field(default=SecretStr(INSECURE_EXAMPLE_KEK))
    admin_bootstrap_token: SecretStr = Field(default=SecretStr(INSECURE_BOOTSTRAP_TOKEN))
    store_prompts: bool = False
    allow_local_hosts: str = "localhost:11434,127.0.0.1:11434,ollama:11434"
    # Data-plane auth: when True, every /v1/* request must present a Gateway client key (ugw_live_...).
    # When False (default), requests without a client key run in BYOK pass-through mode.
    require_client_api_key: bool = False
    # Background usage writer (keeps DB INSERT/COMMIT off the request hot path)
    usage_flush_interval_seconds: float = Field(default=0.25, gt=0.0, le=60.0)
    usage_flush_batch_size: int = Field(default=100, ge=1, le=10000)
    usage_queue_max_size: int = Field(default=10000, ge=1, le=1_000_000)
    # Prometheus label-cardinality guard
    metrics_max_series: int = Field(default=2000, ge=10, le=1_000_000)

    # PostgreSQL / Database Configuration
    database_url: str = (
        "postgresql+asyncpg://ugw_user:ugw_password@localhost:5432/universal_ai_gateway"
    )
    db_pool_size: int = Field(default=20, ge=1, le=500)
    db_max_overflow: int = Field(default=10, ge=0, le=500)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    db_echo: bool = False

    # Redis Configuration
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=50, ge=1, le=2000)
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    redis_fail_open: bool = True

    # HTTPX Client Pool & Explicit Timeout Configuration
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0.0, le=120.0)
    http_read_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    http_write_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    http_pool_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    http_max_connections: int = Field(default=200, ge=1, le=5000)
    http_max_keepalive_connections: int = Field(default=50, ge=0, le=2000)
    http_keepalive_expiry_seconds: float = Field(default=30.0, gt=0.0, le=600.0)

    # Default Routing Weights (Quality 30%, Reliability 25%, Cost 20%, Latency 20%, Capacity 5%)
    default_weight_quality: float = Field(default=0.30, ge=0.0, le=1.0)
    default_weight_reliability: float = Field(default=0.25, ge=0.0, le=1.0)
    default_weight_cost: float = Field(default=0.20, ge=0.0, le=1.0)
    default_weight_latency: float = Field(default=0.20, ge=0.0, le=1.0)
    default_weight_capacity: float = Field(default=0.05, ge=0.0, le=1.0)

    @field_validator("api_v1_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith("/"):
            raise ValueError("api_v1_prefix must start with '/'")
        return cleaned.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        allowed_prefixes = (
            "postgresql+asyncpg://",
            "sqlite+aiosqlite://",
        )
        if not value.startswith(allowed_prefixes):
            raise ValueError(
                f"database_url must use an async SQLAlchemy driver {allowed_prefixes}"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        allowed_prefixes = ("redis://", "rediss://", "memory://")
        if not value.startswith(allowed_prefixes):
            raise ValueError(f"redis_url must start with one of {allowed_prefixes}")
        return value

    @field_validator("master_kek_base64")
    @classmethod
    def validate_master_kek(cls, value: SecretStr) -> SecretStr:
        raw_str = value.get_secret_value().strip()
        try:
            decoded = base64.urlsafe_b64decode(raw_str)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("master_kek_base64 must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError(
                f"master_kek_base64 must decode to exactly 32 bytes (256 bits), got {len(decoded)} bytes"
            )
        return value

    @model_validator(mode="after")
    def validate_weights_and_production_secrets(self) -> "GatewaySettings":
        weight_sum = (
            self.default_weight_quality
            + self.default_weight_reliability
            + self.default_weight_cost
            + self.default_weight_latency
            + self.default_weight_capacity
        )
        if abs(weight_sum - 1.0) > 1e-5:
            raise ValueError(
                f"Default routing weights must sum to 1.0, got {weight_sum:.6f}"
            )

        if self.env == "production":
            if self.master_kek_base64.get_secret_value() == INSECURE_EXAMPLE_KEK:
                raise ValueError(
                    "Refusing to start in production with the default example GATEWAY_MASTER_KEK_BASE64"
                )
            if self.admin_bootstrap_token.get_secret_value() == INSECURE_BOOTSTRAP_TOKEN:
                raise ValueError(
                    "Refusing to start in production with the default example GATEWAY_ADMIN_BOOTSTRAP_TOKEN"
                )
        return self

    def get_master_kek_bytes(self) -> bytes:
        """Returns the decoded 32-byte Key Encryption Key."""
        return base64.urlsafe_b64decode(self.master_kek_base64.get_secret_value().strip())

    @property
    def allowed_local_hosts_set(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.allow_local_hosts.split(",")
            if item.strip()
        }


@lru_cache(maxsize=1)
def get_settings() -> GatewaySettings:
    """Returns cached singleton settings instance."""
    return GatewaySettings()

