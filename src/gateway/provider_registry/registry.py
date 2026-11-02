"""Provider Registry — lifecycle, capability discovery, health tracking, and custom plugins."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from gateway.capability_registry.models import ProtocolType, ProviderCapabilityManifest
from gateway.core.canonical import (
    AdapterCredential,
    DiscoveredModel,
    HealthCheckResult,
    HealthStatus,
)
from gateway.core.exceptions import ConfigurationError, ResourceNotFoundError
from gateway.core.http import HTTPXClientPoolManager
from gateway.core.logging import get_logger
from gateway.provider_adapters import (
    BUILTIN_ADAPTER_CLASSES,
    BaseProviderAdapter,
    ConfigValidationResult,
    CustomHTTPProviderAdapter,
    ProviderConfig,
)

logger = get_logger("gateway.provider_registry")


class ProviderSummary(BaseModel):
    """Public summary of a registered provider and its health/capability status."""

    provider_id: str
    display_name: str
    adapter_type: str
    base_url: str
    enabled: bool
    supported_protocols: list[ProtocolType]
    health_status: HealthStatus = HealthStatus.UNKNOWN
    last_health_check: datetime | None = None
    manifest: ProviderCapabilityManifest
    discovered_models_count: int = 0


class ProviderRegistry:
    """Central registry managing built-in and custom AI provider adapters."""

    def __init__(self, http_pool: HTTPXClientPoolManager) -> None:
        self._http_pool = http_pool
        self._adapters: dict[str, BaseProviderAdapter] = {}
        self._health_cache: dict[str, HealthCheckResult] = {}
        self._discovered_models_cache: dict[str, list[DiscoveredModel]] = {}
        self._lock = asyncio.Lock()

    def register_builtin_adapters(self) -> None:
        """Instantiates and registers all 7 verified built-in provider adapters."""
        for adapter_cls in BUILTIN_ADAPTER_CLASSES:
            adapter = adapter_cls(http_pool=self._http_pool)
            self._adapters[adapter.provider_id] = adapter
            self._health_cache.setdefault(
                adapter.provider_id,
                HealthCheckResult(
                    provider_id=adapter.provider_id,
                    status=HealthStatus.UNKNOWN,
                    message="Not yet probed",
                ),
            )
        logger.info(
            "builtin_provider_adapters_registered",
            providers=sorted(self._adapters.keys()),
        )

    def register_adapter(self, adapter: BaseProviderAdapter) -> ConfigValidationResult:
        """Validates and registers a BaseProviderAdapter instance."""
        validation = adapter.validate_configuration()
        if not validation.valid:
            raise ConfigurationError(
                f"Invalid configuration for provider {adapter.provider_id!r}: {'; '.join(validation.errors)}",
                details={"errors": validation.errors, "warnings": validation.warnings},
            )
        self._adapters[adapter.provider_id] = adapter
        self._health_cache[adapter.provider_id] = HealthCheckResult(
            provider_id=adapter.provider_id,
            status=HealthStatus.UNKNOWN,
            message="Registered",
        )
        logger.info(
            "provider_adapter_registered",
            provider_id=adapter.provider_id,
            adapter_type=adapter.adapter_type,
        )
        return validation

    async def register_custom_provider(
        self, config: ProviderConfig
    ) -> ProviderSummary:
        """Creates, validates, starts, and registers a CustomHTTPProviderAdapter."""
        async with self._lock:
            adapter = CustomHTTPProviderAdapter(
                http_pool=self._http_pool, config=config
            )
            self.register_adapter(adapter)
            await adapter.startup()
            return self.get_provider_summary(adapter.provider_id)

    async def unregister_provider(self, provider_id: str) -> None:
        """Shuts down and removes a registered provider adapter."""
        async with self._lock:
            adapter = self._adapters.pop(provider_id, None)
            if adapter is None:
                raise ResourceNotFoundError(
                    f"Provider {provider_id!r} is not registered",
                    param="provider_id",
                )
            await adapter.shutdown()
            self._health_cache.pop(provider_id, None)
            self._discovered_models_cache.pop(provider_id, None)
            logger.info("provider_adapter_unregistered", provider_id=provider_id)

    def get_adapter(self, provider_id: str) -> BaseProviderAdapter:
        """Retrieves a registered adapter by provider_id or raises ResourceNotFoundError."""
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise ResourceNotFoundError(
                f"Provider {provider_id!r} is not registered",
                param="provider_id",
            )
        return adapter

    def has_provider(self, provider_id: str) -> bool:
        return provider_id in self._adapters

    def list_adapters(self, *, enabled_only: bool = False) -> list[BaseProviderAdapter]:
        adapters = list(self._adapters.values())
        if enabled_only:
            adapters = [a for a in adapters if a.config.enabled]
        return sorted(adapters, key=lambda a: a.provider_id)

    def get_capability_manifest(self, provider_id: str) -> ProviderCapabilityManifest:
        return self.get_adapter(provider_id).get_capability_manifest()

    def get_provider_summary(self, provider_id: str) -> ProviderSummary:
        adapter = self.get_adapter(provider_id)
        health = self._health_cache.get(provider_id)
        discovered = self._discovered_models_cache.get(provider_id, [])
        manifest = adapter.get_capability_manifest()
        return ProviderSummary(
            provider_id=adapter.provider_id,
            display_name=adapter.display_name,
            adapter_type=adapter.adapter_type,
            base_url=adapter.base_url,
            enabled=adapter.config.enabled,
            supported_protocols=list(adapter.supported_protocols),
            health_status=health.status if health else HealthStatus.UNKNOWN,
            last_health_check=health.checked_at if health else None,
            manifest=manifest,
            discovered_models_count=len(discovered),
        )

    def list_provider_summaries(
        self, *, enabled_only: bool = False
    ) -> list[ProviderSummary]:
        return [
            self.get_provider_summary(a.provider_id)
            for a in self.list_adapters(enabled_only=enabled_only)
        ]

    def validate_provider_config(
        self, provider_id: str, config: ProviderConfig | None = None
    ) -> ConfigValidationResult:
        adapter = self.get_adapter(provider_id)
        return adapter.validate_configuration(config)

    async def check_provider_health(
        self,
        provider_id: str,
        credential: AdapterCredential | None = None,
    ) -> HealthCheckResult:
        adapter = self.get_adapter(provider_id)
        result = await adapter.check_health(credential=credential)
        self._health_cache[provider_id] = result
        return result

    def get_cached_health(self, provider_id: str) -> HealthCheckResult:
        self.get_adapter(provider_id)
        return self._health_cache.get(
            provider_id,
            HealthCheckResult(
                provider_id=provider_id,
                status=HealthStatus.UNKNOWN,
                message="Not yet probed",
            ),
        )

    async def discover_provider_models(
        self,
        provider_id: str,
        credential: AdapterCredential | None = None,
    ) -> list[DiscoveredModel]:
        adapter = self.get_adapter(provider_id)
        models = await adapter.discover_models(credential=credential)
        self._discovered_models_cache[provider_id] = models
        self._health_cache[provider_id] = HealthCheckResult(
            provider_id=provider_id,
            status=HealthStatus.HEALTHY,
            message=f"Healthy ({len(models)} models discovered)",
            details={"discovered_model_count": len(models)},
        )
        return models

    def get_cached_models(self, provider_id: str) -> list[DiscoveredModel]:
        self.get_adapter(provider_id)
        return list(self._discovered_models_cache.get(provider_id, []))

    async def startup_all(self) -> None:
        """Starts all registered provider adapters."""
        for adapter in self._adapters.values():
            await adapter.startup()

    async def shutdown_all(self) -> None:
        """Gracefully shuts down all registered provider adapters."""
        for adapter in self._adapters.values():
            await adapter.shutdown()

