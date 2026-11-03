"""Redis async connection pool management with explicit timeouts and fallback support."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from redis.asyncio import ConnectionPool, Redis

from gateway.config.settings import GatewaySettings
from gateway.core.logging import get_logger

logger = get_logger("gateway.redis")


class InMemoryRedisFallback:
    """Thread/async-safe local in-memory Redis subset used for memory:// or degraded fail-open mode."""

    def __init__(self) -> None:
        self._kv: dict[str, tuple[Any, float | None]] = {}
        self._hashes: dict[str, dict[str, Any]] = {}
        self._sets: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, key: str) -> bool:
        entry = self._kv.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            self._kv.pop(key, None)
            return True
        return False

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        async with self._lock:
            if self._is_expired(key):
                return None
            entry = self._kv.get(key)
            return str(entry[0]) if entry is not None else None

    async def set(
        self,
        key: str,
        value: str | int | float,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        async with self._lock:
            self._is_expired(key)
            if nx and key in self._kv:
                return False
            expires_at: float | None = None
            if ex is not None:
                expires_at = time.monotonic() + float(ex)
            elif px is not None:
                expires_at = time.monotonic() + (float(px) / 1000.0)
            self._kv[key] = (str(value), expires_at)
            return True

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            deleted = 0
            for key in keys:
                if self._kv.pop(key, None) is not None:
                    deleted += 1
                if self._hashes.pop(key, None) is not None:
                    deleted += 1
                if self._sets.pop(key, None) is not None:
                    deleted += 1
            return deleted

    async def incr(self, key: str) -> int:
        async with self._lock:
            self._is_expired(key)
            current, expires_at = self._kv.get(key, ("0", None))
            new_val = int(current) + 1
            self._kv[key] = (str(new_val), expires_at)
            return new_val

    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            if self._is_expired(key) or key not in self._kv:
                return False
            val, _ = self._kv[key]
            self._kv[key] = (val, time.monotonic() + float(seconds))
            return True

    async def hset(
        self,
        name: str,
        key: str | None = None,
        value: Any = None,
        mapping: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            bucket = self._hashes.setdefault(name, {})
            added = 0
            if key is not None:
                if key not in bucket:
                    added += 1
                bucket[key] = str(value)
            if mapping:
                for k, v in mapping.items():
                    if k not in bucket:
                        added += 1
                    bucket[k] = str(v)
            return added

    async def hget(self, name: str, key: str) -> str | None:
        async with self._lock:
            bucket = self._hashes.get(name, {})
            return bucket.get(key)

    async def hgetall(self, name: str) -> dict[str, str]:
        async with self._lock:
            return dict(self._hashes.get(name, {}))

    async def sadd(self, name: str, *values: str) -> int:
        async with self._lock:
            bucket = self._sets.setdefault(name, set())
            before = len(bucket)
            bucket.update(str(v) for v in values)
            return len(bucket) - before

    async def srem(self, name: str, *values: str) -> int:
        async with self._lock:
            bucket = self._sets.get(name)
            if not bucket:
                return 0
            removed = 0
            for v in values:
                if str(v) in bucket:
                    bucket.remove(str(v))
                    removed += 1
            return removed

    async def scard(self, name: str) -> int:
        async with self._lock:
            return len(self._sets.get(name, set()))

    async def aclose(self) -> None:
        async with self._lock:
            self._kv.clear()
            self._hashes.clear()
            self._sets.clear()



class RedisManager:
    """Manages Redis async connection pooling, health checks, and graceful shutdown."""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._client: Redis | InMemoryRedisFallback | None = None
        self._using_memory_fallback: bool = False

    def init_client(self) -> Redis | InMemoryRedisFallback:
        """Initializes the Redis client or in-memory store with explicit timeouts."""
        if self._client is not None:
            return self._client

        if self._settings.redis_url.startswith("memory://"):
            self._using_memory_fallback = True
            self._client = InMemoryRedisFallback()
            logger.info("redis_initialized_in_memory_mode")
            return self._client

        self._pool = ConnectionPool.from_url(
            self._settings.redis_url,
            max_connections=self._settings.redis_max_connections,
            socket_timeout=self._settings.redis_socket_timeout_seconds,
            socket_connect_timeout=self._settings.redis_connect_timeout_seconds,
            decode_responses=True,
        )
        self._client = Redis(connection_pool=self._pool)
        self._using_memory_fallback = False
        logger.info("redis_pool_initialized", max_connections=self._settings.redis_max_connections)
        return self._client

    @property
    def client(self) -> Redis | InMemoryRedisFallback:
        if self._client is None:
            return self.init_client()
        return self._client

    async def check_health(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Pings Redis within an explicit timeout and reports health status."""
        timeout = timeout_seconds or self._settings.redis_socket_timeout_seconds
        client = self.client
        try:
            async with asyncio.timeout(timeout):
                pong = await client.ping()
                return {
                    "status": "healthy" if pong else "unhealthy",
                    "backend": "memory" if self._using_memory_fallback else "redis",
                }
        except Exception as exc:
            logger.warning("redis_health_check_failed", error=str(exc))
            if self._settings.redis_fail_open:
                return {
                    "status": "degraded",
                    "backend": "redis_fail_open",
                    "error": type(exc).__name__,
                }
            return {
                "status": "unhealthy",
                "backend": "redis",
                "error": type(exc).__name__,
            }

    async def close(self) -> None:
        """Gracefully closes Redis client and connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None
        logger.info("redis_pool_closed")

