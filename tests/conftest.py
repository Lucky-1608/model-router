"""Shared pytest fixtures for Universal AI Gateway tests."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
import os

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from gateway.config import GatewaySettings
from gateway.main import create_app


@pytest.fixture
def test_settings() -> GatewaySettings:
    """Provides isolated test settings using async SQLite in-memory and memory:// Redis."""
    test_kek = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    return GatewaySettings(
        env="test",
        app_name="Universal AI Gateway Test",
        version="0.1.0-test",
        log_level="DEBUG",
        log_format="json",
        master_kek_base64=test_kek,
        admin_bootstrap_token="ugw_admin_test_token_1234567890",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="memory://localhost",
        http_connect_timeout_seconds=3.0,
        http_read_timeout_seconds=15.0,
        http_write_timeout_seconds=5.0,
        http_pool_timeout_seconds=2.0,
    )


@pytest_asyncio.fixture
async def test_app(test_settings: GatewaySettings) -> AsyncIterator[FastAPI]:
    """Creates a FastAPI app instance and runs its lifespan startup/shutdown hooks."""
    app = create_app(settings_override=test_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def async_client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Provides an HTTPX AsyncClient wired to the ASGI test application."""
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client

