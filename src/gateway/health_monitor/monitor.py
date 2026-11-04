"""Health Monitor & Redis-Coordinated Circuit Breaker Engine."""

from __future__ import annotations

import time

from gateway.core.canonical import HealthCheckResult, HealthStatus
from gateway.core.redis import RedisManager
from gateway.health_monitor.models import (
    CircuitBreakerConfig,
    CircuitBreakerSnapshot,
    CircuitBreakerState,
    CircuitScope,
)
from gateway.model_registry.registry import ModelRegistry
from gateway.provider_registry.registry import ProviderRegistry


class HealthMonitor:
    """Tracks passive/active health and manages circuit breakers across providers, models, and credentials."""

    def __init__(
        self,
        redis_manager: RedisManager,
        provider_registry: ProviderRegistry,
        model_registry: ModelRegistry,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._redis_manager = redis_manager
        self._provider_registry = provider_registry
        self._model_registry = model_registry
        self._config = config or CircuitBreakerConfig()
        self._known_targets: set[tuple[CircuitScope, str]] = set()

    @property
    def redis(self):
        return self._redis_manager.client

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    def _key_prefix(self, scope: CircuitScope, target_id: str) -> str:
        return f"ugw:cb:{scope.value}:{target_id}"

    async def get_circuit_snapshot(
        self, scope: CircuitScope, target_id: str
    ) -> CircuitBreakerSnapshot:
        """Retrieves the current circuit breaker snapshot, transitioning OPEN -> HALF_OPEN if cooldown expired."""
        self._known_targets.add((scope, target_id))
        prefix = self._key_prefix(scope, target_id)
        raw_state = await self.redis.get(f"{prefix}:state")
        raw_failures = await self.redis.get(f"{prefix}:failures")
        raw_opened_until = await self.redis.get(f"{prefix}:opened_until")
        raw_last_err = await self.redis.get(f"{prefix}:last_error")

        state = (
            CircuitBreakerState(raw_state)
            if raw_state in CircuitBreakerState._value2member_map_
            else CircuitBreakerState.CLOSED
        )
        failures = int(raw_failures) if raw_failures and str(raw_failures).isdigit() else 0
        opened_until = float(raw_opened_until) if raw_opened_until else None
        now = time.time()
        remaining = 0.0

        if state == CircuitBreakerState.OPEN:
            if opened_until is not None and now >= opened_until:
                state = CircuitBreakerState.HALF_OPEN
                await self.redis.set(
                    f"{prefix}:state",
                    CircuitBreakerState.HALF_OPEN.value,
                    ex=self._config.rolling_window_seconds,
                )
                self._sync_registry_health(scope, target_id, HealthStatus.DEGRADED)
            elif opened_until is not None:
                remaining = round(max(opened_until - now, 0.0), 3)

        health = (
            HealthStatus.UNHEALTHY
            if state == CircuitBreakerState.OPEN
            else (
                HealthStatus.DEGRADED
                if state == CircuitBreakerState.HALF_OPEN or failures > 0
                else HealthStatus.HEALTHY
            )
        )
        return CircuitBreakerSnapshot(
            scope=scope,
            target_id=target_id,
            state=state,
            failure_count=failures,
            opened_until_epoch=opened_until,
            remaining_open_seconds=remaining,
            last_error_code=raw_last_err,
            health_status=health,
        )

    async def is_request_allowed(
        self, scope: CircuitScope, target_id: str
    ) -> tuple[bool, CircuitBreakerSnapshot]:
        """Checks whether a request is allowed through the circuit breaker.

        If the circuit is HALF_OPEN, uses an atomic Redis SET NX probe lock to ensure only
        `half_open_max_probes` concurrent caller is admitted through to test recovery, preventing
        thundering herd on recovering upstreams.
        """
        snap = await self.get_circuit_snapshot(scope, target_id)
        if snap.state == CircuitBreakerState.OPEN:
            return False, snap

        if snap.state == CircuitBreakerState.HALF_OPEN:
            prefix = self._key_prefix(scope, target_id)
            probe_lock_key = f"{prefix}:probe_lock"
            won_lock = await self.redis.set(
                probe_lock_key, "1", ex=int(self._config.recovery_timeout_seconds), nx=True
            )
            if not won_lock:
                # Another concurrent request is already probing; reject this one to prevent thundering herd
                return False, snap

        return True, snap

    async def record_success(
        self,
        *,
        provider_id: str,
        model_id: str | None = None,
        credential_id: str | None = None,
        latency_ms: float | None = None,
        ttft_ms: float | None = None,
    ) -> None:
        """Records a successful upstream call, closing HALF_OPEN circuits and updating EWMA latency."""
        await self._reset_circuit(CircuitScope.PROVIDER, provider_id)
        if model_id:
            canonical_model_id = (
                model_id
                if "/" in model_id
                else f"{provider_id}/{model_id}"
            )
            await self._reset_circuit(CircuitScope.MODEL, canonical_model_id)
            self._update_model_latency_and_reliability(
                canonical_model_id,
                success=True,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
            )
        if credential_id:
            await self._reset_circuit(CircuitScope.CREDENTIAL, credential_id)

    async def record_failure(
        self,
        *,
        provider_id: str,
        model_id: str | None = None,
        credential_id: str | None = None,
        error_code: str = "GW_UPSTREAM_ERROR",
        trip_circuit: bool = True,
    ) -> CircuitBreakerSnapshot:
        """Records an upstream failure and trips the circuit breaker to OPEN if threshold is reached."""
        prov_snap = await self._increment_failure(
            CircuitScope.PROVIDER,
            provider_id,
            error_code=error_code,
            trip_circuit=trip_circuit,
        )
        if model_id:
            canonical_model_id = (
                model_id
                if "/" in model_id
                else f"{provider_id}/{model_id}"
            )
            await self._increment_failure(
                CircuitScope.MODEL,
                canonical_model_id,
                error_code=error_code,
                trip_circuit=trip_circuit,
            )
            self._update_model_latency_and_reliability(
                canonical_model_id,
                success=False,
                latency_ms=None,
                ttft_ms=None,
            )
        if credential_id:
            await self._increment_failure(
                CircuitScope.CREDENTIAL,
                credential_id,
                error_code=error_code,
                trip_circuit=trip_circuit,
            )
        return prov_snap

    async def _reset_circuit(self, scope: CircuitScope, target_id: str) -> None:
        self._known_targets.add((scope, target_id))
        prefix = self._key_prefix(scope, target_id)
        await self.redis.delete(
            f"{prefix}:state",
            f"{prefix}:failures",
            f"{prefix}:opened_until",
            f"{prefix}:last_error",
            f"{prefix}:probe_lock",
        )
        self._sync_registry_health(scope, target_id, HealthStatus.HEALTHY)

    async def _increment_failure(
        self,
        scope: CircuitScope,
        target_id: str,
        *,
        error_code: str,
        trip_circuit: bool,
    ) -> CircuitBreakerSnapshot:
        self._known_targets.add((scope, target_id))
        prefix = self._key_prefix(scope, target_id)
        snap = await self.get_circuit_snapshot(scope, target_id)

        failures = await self.redis.incr(f"{prefix}:failures")
        await self.redis.expire(
            f"{prefix}:failures", self._config.rolling_window_seconds
        )
        await self.redis.set(
            f"{prefix}:last_error",
            error_code,
            ex=self._config.rolling_window_seconds,
        )

        should_open = trip_circuit and (
            snap.state == CircuitBreakerState.HALF_OPEN
            or failures >= self._config.failure_threshold
        )
        if should_open:
            opened_until = time.time() + self._config.recovery_timeout_seconds
            ttl = int(self._config.recovery_timeout_seconds) + 60
            await self.redis.set(
                f"{prefix}:state", CircuitBreakerState.OPEN.value, ex=ttl
            )
            await self.redis.set(
                f"{prefix}:opened_until", str(opened_until), ex=ttl
            )
            await self.redis.delete(f"{prefix}:probe_lock")
            self._sync_registry_health(scope, target_id, HealthStatus.UNHEALTHY)
        else:
            self._sync_registry_health(scope, target_id, HealthStatus.DEGRADED)

        return await self.get_circuit_snapshot(scope, target_id)

    def _sync_registry_health(
        self, scope: CircuitScope, target_id: str, status: HealthStatus
    ) -> None:
        if scope == CircuitScope.MODEL:
            try:
                self._model_registry.update_model_health(target_id, status)
            except Exception:
                pass

    def _update_model_latency_and_reliability(
        self,
        canonical_model_id: str,
        *,
        success: bool,
        latency_ms: float | None,
        ttft_ms: float | None,
    ) -> None:
        try:
            meta = self._model_registry.get_model(canonical_model_id)
        except Exception:
            return

        # EWMA reliability update (alpha = 0.05)
        sample_val = 1.0 if success else 0.0
        meta.reliability_score = round(
            (0.95 * meta.reliability_score) + (0.05 * sample_val), 4
        )
        if latency_ms is not None and latency_ms > 0:
            if meta.latency.p50_ms is None:
                meta.latency.p50_ms = round(latency_ms, 2)
                meta.latency.p95_ms = round(latency_ms * 1.5, 2)
            else:
                meta.latency.p50_ms = round(
                    (0.85 * meta.latency.p50_ms) + (0.15 * latency_ms), 2
                )
                meta.latency.p95_ms = round(
                    max(meta.latency.p50_ms * 1.25, (0.85 * (meta.latency.p95_ms or latency_ms)) + (0.15 * latency_ms)),
                    2,
                )
            meta.latency.sample_count += 1
        if ttft_ms is not None and ttft_ms > 0:
            if meta.latency.ttft_ms is None:
                meta.latency.ttft_ms = round(ttft_ms, 2)
            else:
                meta.latency.ttft_ms = round(
                    (0.85 * meta.latency.ttft_ms) + (0.15 * ttft_ms), 2
                )

    async def probe_provider_health(self, provider_id: str) -> HealthCheckResult:
        """Executes an active health check against a provider and updates its circuit state."""
        res = await self._provider_registry.check_provider_health(provider_id)
        if res.status == HealthStatus.HEALTHY:
            await self.record_success(
                provider_id=provider_id, latency_ms=res.latency_ms
            )
        elif res.status == HealthStatus.UNHEALTHY:
            await self.record_failure(
                provider_id=provider_id,
                error_code=res.error_message or "HEALTH_CHECK_FAILED",
            )
        return res

    async def reset_circuit(
        self, scope: CircuitScope, target_id: str
    ) -> CircuitBreakerSnapshot:
        """Public method to reset a circuit breaker back to CLOSED state."""
        await self._reset_circuit(scope, target_id)
        return await self.get_circuit_snapshot(scope, target_id)

    async def list_known_circuits(
        self, scope: CircuitScope | None = None
    ) -> list[CircuitBreakerSnapshot]:
        snapshots: list[CircuitBreakerSnapshot] = []
        for sc, target_id in sorted(
            self._known_targets, key=lambda x: (x[0].value, x[1])
        ):
            if scope is not None and sc != scope:
                continue
            snapshots.append(await self.get_circuit_snapshot(sc, target_id))
        return snapshots

    async def list_all_circuits(self) -> list[CircuitBreakerSnapshot]:
        return await self.list_known_circuits()
