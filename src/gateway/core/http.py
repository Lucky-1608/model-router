"""HTTPX async connection pool manager with explicit timeout and limit policies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.config.settings import GatewaySettings
from gateway.core.logging import get_logger

logger = get_logger("gateway.http")


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """Explicit four-phase HTTP timeout configuration."""

    connect: float
    read: float
    write: float
    pool: float

    def to_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )


class HTTPXClientPoolManager:
    """Manages shared and per-provider HTTPX AsyncClient connection pools."""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def default_timeout_policy(self) -> TimeoutPolicy:
        return TimeoutPolicy(
            connect=self._settings.http_connect_timeout_seconds,
            read=self._settings.http_read_timeout_seconds,
            write=self._settings.http_write_timeout_seconds,
            pool=self._settings.http_pool_timeout_seconds,
        )

    @property
    def default_limits(self) -> httpx.Limits:
        return httpx.Limits(
            max_connections=self._settings.http_max_connections,
            max_keepalive_connections=self._settings.http_max_keepalive_connections,
            keepalive_expiry=self._settings.http_keepalive_expiry_seconds,
        )

    def _build_client(
        self,
        *,
        base_url: str | None = None,
        timeout_policy: TimeoutPolicy | None = None,
    ) -> httpx.AsyncClient:
        policy = timeout_policy or self.default_timeout_policy
        client_kwargs: dict[str, Any] = {
            "timeout": policy.to_httpx_timeout(),
            "limits": self.default_limits,
            "follow_redirects": False,
            "headers": {
                "User-Agent": f"UniversalAIGateway/{self._settings.version}",
            },
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        return httpx.AsyncClient(**client_kwargs)

    async def get_client(
        self,
        pool_key: str = "default",
        *,
        base_url: str | None = None,
        timeout_policy: TimeoutPolicy | None = None,
    ) -> httpx.AsyncClient:
        """Retrieves or lazily initializes a pooled HTTPX AsyncClient for the given pool_key."""
        async with self._lock:
            if self._closed:
                self._closed = False
            existing = self._clients.get(pool_key)
            if existing is not None and not existing.is_closed:
                return existing
            client = self._build_client(base_url=base_url, timeout_policy=timeout_policy)
            self._clients[pool_key] = client
            logger.debug(
                "httpx_client_pool_created",
                pool_key=pool_key,
                base_url=base_url,
                connect_timeout=self.default_timeout_policy.connect,
                read_timeout=self.default_timeout_policy.read,
            )
            return client

    def get_pool_stats(self) -> dict[str, Any]:
        """Returns diagnostic metadata about active HTTPX client pools."""
        return {
            "active_pools": [
                key for key, client in self._clients.items() if not client.is_closed
            ],
            "pool_count": sum(1 for c in self._clients.values() if not c.is_closed),
            "default_timeouts": {
                "connect": self._settings.http_connect_timeout_seconds,
                "read": self._settings.http_read_timeout_seconds,
                "write": self._settings.http_write_timeout_seconds,
                "pool": self._settings.http_pool_timeout_seconds,
            },
            "limits": {
                "max_connections": self._settings.http_max_connections,
                "max_keepalive_connections": self._settings.http_max_keepalive_connections,
                "keepalive_expiry": self._settings.http_keepalive_expiry_seconds,
            },
        }

    async def aclose_all(self) -> None:
        """Gracefully closes all managed HTTPX AsyncClient pools."""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._closed = True
        for client in clients:
            if not client.is_closed:
                await client.aclose()
        logger.info("httpx_client_pools_closed", closed_count=len(clients))

