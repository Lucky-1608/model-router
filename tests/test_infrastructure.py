"""Tests for DatabaseManager, RedisManager, HTTPXClientPoolManager, and secret redaction."""

from __future__ import annotations

from sqlalchemy import text

from gateway.config import GatewaySettings
from gateway.core import (
    DatabaseManager,
    HTTPXClientPoolManager,
    RedisManager,
    TimeoutPolicy,
    redact_sensitive_data,
)


async def test_database_connection_and_session_lifecycle(
    test_settings: GatewaySettings,
) -> None:
    db = DatabaseManager(test_settings)
    db.init_engine()
    try:
        health = await db.check_health()
        assert health["status"] == "healthy"
        assert health["dialect"] == "sqlite"

        async for session in db.get_session():
            result = await session.execute(text("SELECT 42"))
            assert result.scalar_one() == 42
    finally:
        await db.dispose()


async def test_redis_connection_and_operations(test_settings: GatewaySettings) -> None:
    redis_mgr = RedisManager(test_settings)
    client = redis_mgr.init_client()
    try:
        health = await redis_mgr.check_health()
        assert health["status"] == "healthy"

        # Key-value & NX
        assert await client.set("ugw:test:key", "val1", ex=60, nx=True) is True
        assert await client.set("ugw:test:key", "val2", ex=60, nx=True) is False
        assert await client.get("ugw:test:key") == "val1"

        # Atomic counter
        assert await client.incr("ugw:test:counter") == 1
        assert await client.incr("ugw:test:counter") == 2

        # Hash operations
        await client.hset("ugw:test:hash", mapping={"status": "ACTIVE", "weight": "100"})
        assert await client.hget("ugw:test:hash", "status") == "ACTIVE"
        assert await client.hgetall("ugw:test:hash") == {
            "status": "ACTIVE",
            "weight": "100",
        }

        # Set operations for concurrency tracking
        assert await client.sadd("ugw:test:inflight", "req-1", "req-2") == 2
        assert await client.scard("ugw:test:inflight") == 2
        assert await client.srem("ugw:test:inflight", "req-1") == 1
        assert await client.scard("ugw:test:inflight") == 1
    finally:
        await redis_mgr.close()


async def test_httpx_client_pool_and_explicit_timeouts(
    test_settings: GatewaySettings,
) -> None:
    pool = HTTPXClientPoolManager(test_settings)
    try:
        default_client = await pool.get_client("default")
        openai_client = await pool.get_client(
            "openai",
            base_url="https://api.openai.com",
            timeout_policy=TimeoutPolicy(connect=2.0, read=30.0, write=10.0, pool=2.0),
        )

        assert default_client is not openai_client
        assert default_client.timeout.connect == 3.0
        assert default_client.timeout.read == 15.0
        assert openai_client.timeout.read == 30.0

        # Requesting the same pool key returns the existing pooled client
        cached_openai_client = await pool.get_client("openai")
        assert cached_openai_client is openai_client

        stats = pool.get_pool_stats()
        assert stats["pool_count"] == 2
        assert set(stats["active_pools"]) == {"default", "openai"}
    finally:
        await pool.aclose_all()
        assert pool.get_pool_stats()["pool_count"] == 0


def test_secret_redaction_processor() -> None:
    payload = {
        "Authorization": "Bearer sk-proj-1234567890abcdef1234567890",
        "x-api-key": "sk-ant-api03-secretkeyvalue123456789",
        "nested": {
            "message": "Failed with key sk-1234567890abcdefghijklmnop and gsk_1234567890abcdef",
            "api_key": "raw_secret_value",
            "safe_field": "gpt-4o",
        },
    }
    redacted = redact_sensitive_data(payload)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["x-api-key"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "gpt-4o"
    assert "sk-" not in redacted["nested"]["message"]
    assert "gsk_" not in redacted["nested"]["message"]
    assert "[REDACTED_SECRET]" in redacted["nested"]["message"]

