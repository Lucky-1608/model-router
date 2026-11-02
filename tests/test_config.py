"""Tests for GatewaySettings validation and security guardrails."""

from __future__ import annotations

import base64

from pydantic import ValidationError
import pytest

from gateway.config.settings import (
    INSECURE_BOOTSTRAP_TOKEN,
    INSECURE_EXAMPLE_KEK,
    GatewaySettings,
)


def test_default_settings_valid() -> None:
    settings = GatewaySettings()
    assert settings.env == "development"
    assert len(settings.get_master_kek_bytes()) == 32
    assert settings.default_weight_quality == 0.30
    assert settings.default_weight_reliability == 0.25
    assert settings.default_weight_cost == 0.20
    assert settings.default_weight_latency == 0.20
    assert settings.default_weight_capacity == 0.05


def test_invalid_master_kek_length_rejected() -> None:
    short_key = base64.urlsafe_b64encode(b"too_short_16byte").decode()
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        GatewaySettings(master_kek_base64=short_key)


def test_invalid_routing_weights_sum_rejected() -> None:
    with pytest.raises(ValidationError, match="Default routing weights must sum to 1.0"):
        GatewaySettings(
            default_weight_quality=0.50,
            default_weight_reliability=0.25,
            default_weight_cost=0.20,
            default_weight_latency=0.20,
            default_weight_capacity=0.05,
        )


def test_production_rejects_default_example_kek() -> None:
    with pytest.raises(
        ValidationError,
        match="Refusing to start in production with the default example GATEWAY_MASTER_KEK_BASE64",
    ):
        GatewaySettings(
            env="production",
            master_kek_base64=INSECURE_EXAMPLE_KEK,
            admin_bootstrap_token="ugw_admin_prod_custom_secret_token_999",
        )


def test_production_rejects_default_bootstrap_token() -> None:
    custom_kek = base64.urlsafe_b64encode(b"A" * 32).decode()
    with pytest.raises(
        ValidationError,
        match="Refusing to start in production with the default example GATEWAY_ADMIN_BOOTSTRAP_TOKEN",
    ):
        GatewaySettings(
            env="production",
            master_kek_base64=custom_kek,
            admin_bootstrap_token=INSECURE_BOOTSTRAP_TOKEN,
        )


def test_invalid_database_driver_rejected() -> None:
    with pytest.raises(ValidationError, match="database_url must use an async SQLAlchemy driver"):
        GatewaySettings(database_url="postgresql://sync_driver@localhost/db")


def test_invalid_redis_url_rejected() -> None:
    with pytest.raises(ValidationError, match="redis_url must start with one of"):
        GatewaySettings(redis_url="http://localhost:6379")


def test_non_positive_timeout_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewaySettings(http_connect_timeout_seconds=0.0)

