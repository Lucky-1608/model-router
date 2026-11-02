"""Resilient Retry and Cross-Provider Fallback Execution Engine."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import time

from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalStreamEvent,
)
from gateway.core.exceptions import (
    GatewayError,
    RateLimitExceededError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UnsupportedCapabilityError,
    UpstreamProviderError,
)
from gateway.credential_manager.manager import CredentialManager
from gateway.health_monitor.models import CircuitScope
from gateway.health_monitor.monitor import HealthMonitor
from gateway.model_registry.models import ModelMetadata
from gateway.model_registry.registry import ModelRegistry
from gateway.provider_registry.registry import ProviderRegistry
from gateway.retry_fallback.guard import FallbackCompatibilityGuard
from gateway.retry_fallback.models import ExecutionAttemptTrace, ExecutionOutcome
from gateway.routing_engine.engine import RoutingEngine
from gateway.tool_gateway.models import ToolExecutionMode
from gateway.tool_gateway.orchestrator import ToolGatewayOrchestrator


class RetryFallbackEngine:
    """Executes requests with circuit breaker protection, credential rotation, and capability-verified fallback."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        model_registry: ModelRegistry,
        routing_engine: RoutingEngine,
        health_monitor: HealthMonitor,
        credential_manager: CredentialManager | None = None,
        tool_orchestrator: ToolGatewayOrchestrator | None = None,
        fallback_guard: FallbackCompatibilityGuard | None = None,
    ) -> None:
        self._provider_registry = provider_registry
        self._model_registry = model_registry
        self._routing_engine = routing_engine
        self._health_monitor = health_monitor
        self._credential_manager = credential_manager
        self._tool_orchestrator = tool_orchestrator
        self._guard = fallback_guard or FallbackCompatibilityGuard(
            tool_registry=tool_orchestrator._tool_registry if tool_orchestrator else None
        )

    async def execute_resilient(
        self,
        *,
        request: CanonicalInternalRequest,
        primary_provider: str,
        primary_model: str,
        explicit_credential: AdapterCredential | None = None,
        decision_id: str | None = None,
        allow_fallback: bool = True,
        allow_cross_provider: bool = True,
        tool_mode: ToolExecutionMode = ToolExecutionMode.PASSTHROUGH,
        client_id: str | None = None,
        allowed_tools: set[str] | None = None,
        max_tool_rounds: int | None = None,
    ) -> ExecutionOutcome:
        """Executes a canonical request across the primary target and verified fallback candidates."""
        overall_start = time.perf_counter()
        candidates = self._build_candidate_chain(
            request=request,
            primary_provider=primary_provider,
            primary_model=primary_model,
            decision_id=decision_id,
            allow_fallback=allow_fallback,
            allow_cross_provider=allow_cross_provider,
        )

        attempts: list[ExecutionAttemptTrace] = []
        last_exception: Exception | None = None

        for idx, (cand_provider, cand_model_id, fallback_reason) in enumerate(
            candidates, start=1
        ):
            # 1. If this is a fallback candidate (idx > 1), verify FallbackCompatibilityGuard
            if idx > 1:
                try:
                    cand_meta = self._model_registry.get_model(
                        f"{cand_provider}/{cand_model_id}"
                    )
                except ResourceNotFoundError:
                    cand_meta = None

                if cand_meta is not None:
                    verdict = self._guard.check_compatibility(
                        request=request,
                        primary_provider_id=primary_provider,
                        candidate_model=cand_meta,
                        bytes_sent_to_client=0,
                        non_idempotent_tool_executed=False,
                        allow_cross_provider=allow_cross_provider,
                    )
                    if not verdict.compatible:
                        attempts.append(
                            ExecutionAttemptTrace(
                                attempt_number=idx,
                                provider_id=cand_provider,
                                model_id=cand_model_id,
                                success=False,
                                error_code=verdict.reason_code,
                                error_message=verdict.details,
                                fallback_reason=fallback_reason,
                            )
                        )
                        continue

            # 2. Check Circuit Breakers (Provider + Model)
            canonical_mid = f"{cand_provider}/{cand_model_id}"
            prov_ok, prov_snap = await self._health_monitor.is_request_allowed(
                CircuitScope.PROVIDER, cand_provider
            )
            model_ok, model_snap = await self._health_monitor.is_request_allowed(
                CircuitScope.MODEL, canonical_mid
            )
            if not prov_ok or not model_ok:
                open_reason = (
                    f"Provider circuit {cand_provider!r} is OPEN"
                    if not prov_ok
                    else f"Model circuit {canonical_mid!r} is OPEN"
                )
                attempts.append(
                    ExecutionAttemptTrace(
                        attempt_number=idx,
                        provider_id=cand_provider,
                        model_id=cand_model_id,
                        success=False,
                        error_code="GW_CIRCUIT_OPEN",
                        error_message=open_reason,
                        fallback_reason=fallback_reason,
                    )
                )
                last_exception = ServiceUnavailableError(
                    open_reason,
                    param="model",
                    details={
                        "provider_id": cand_provider,
                        "model_id": cand_model_id,
                        "circuit_state": "OPEN",
                    },
                )
                if not allow_fallback:
                    break
                continue

            # 3. Prepare adapter and model on request
            adapter = self._provider_registry.get_adapter(cand_provider)
            attempt_req = request.model_copy(
                update={
                    "requested_model": cand_model_id,
                    "provider_model_id": cand_model_id,
                },
                deep=True,
            )

            attempt_start = time.perf_counter()
            try:
                resp, used_cred_id = await self._execute_single_candidate(
                    adapter=adapter,
                    request=attempt_req,
                    explicit_credential=explicit_credential
                    if cand_provider == primary_provider
                    else None,
                    tool_mode=tool_mode,
                    client_id=client_id,
                    allowed_tools=allowed_tools,
                    max_tool_rounds=max_tool_rounds,
                )
                attempt_ms = round((time.perf_counter() - attempt_start) * 1000.0, 2)
                await self._health_monitor.record_success(
                    provider_id=cand_provider,
                    model_id=canonical_mid,
                    credential_id=used_cred_id,
                    latency_ms=attempt_ms,
                )
                attempts.append(
                    ExecutionAttemptTrace(
                        attempt_number=idx,
                        provider_id=cand_provider,
                        model_id=cand_model_id,
                        credential_id=used_cred_id,
                        success=True,
                        latency_ms=attempt_ms,
                        fallback_reason=fallback_reason,
                    )
                )
                total_ms = round((time.perf_counter() - overall_start) * 1000.0, 2)
                return ExecutionOutcome(
                    response=resp,
                    selected_provider=cand_provider,
                    selected_model=cand_model_id,
                    credential_id=used_cred_id,
                    decision_id=decision_id,
                    attempts=attempts,
                    fallback_used=(idx > 1),
                    total_latency_ms=total_ms,
                )
            except asyncio.CancelledError:
                # Client cancellation must propagate immediately without retry or fallback
                raise
            except UnsupportedCapabilityError:
                # Capability errors are deterministic client/schema violations; never mask via fallback unless primary had it
                raise
            except GatewayError as exc:
                attempt_ms = round((time.perf_counter() - attempt_start) * 1000.0, 2)
                is_retryable_err = exc.retryable or isinstance(
                    exc,
                    (
                        RateLimitExceededError,
                        UpstreamProviderError,
                        ServiceUnavailableError,
                    ),
                )
                if is_retryable_err:
                    await self._health_monitor.record_failure(
                        provider_id=cand_provider,
                        model_id=canonical_mid,
                        error_code=exc.error_code,
                        trip_circuit=not isinstance(exc, RateLimitExceededError),
                    )
                attempts.append(
                    ExecutionAttemptTrace(
                        attempt_number=idx,
                        provider_id=cand_provider,
                        model_id=cand_model_id,
                        success=False,
                        error_code=exc.error_code,
                        error_message=exc.message,
                        latency_ms=attempt_ms,
                        fallback_reason=fallback_reason,
                    )
                )
                last_exception = exc
                if not is_retryable_err or not allow_fallback:
                    raise

        if isinstance(last_exception, GatewayError):
            last_exception.details["attempts"] = [a.model_dump() for a in attempts]
            raise last_exception
        raise ServiceUnavailableError(
            "All primary and fallback candidates failed or had OPEN circuit breakers",
            details={"attempts": [a.model_dump() for a in attempts]},
        )

    async def execute_stream_resilient(
        self,
        *,
        request: CanonicalInternalRequest,
        primary_provider: str,
        primary_model: str,
        explicit_credential: AdapterCredential | None = None,
        decision_id: str | None = None,
        allow_fallback: bool = True,
        allow_cross_provider: bool = True,
    ) -> tuple[AsyncIterator[CanonicalStreamEvent], str, str, list[ExecutionAttemptTrace]]:
        """Streams a response with pre-first-byte fallback protection and TTFT measurement.

        Once `bytes_sent_to_client > 0` (first chunk yielded), fallback is strictly prohibited.
        """
        candidates = self._build_candidate_chain(
            request=request,
            primary_provider=primary_provider,
            primary_model=primary_model,
            decision_id=decision_id,
            allow_fallback=allow_fallback,
            allow_cross_provider=allow_cross_provider,
        )
        attempts: list[ExecutionAttemptTrace] = []
        last_exc: Exception | None = None

        for idx, (cand_provider, cand_model_id, fb_reason) in enumerate(
            candidates, start=1
        ):
            canonical_mid = f"{cand_provider}/{cand_model_id}"
            prov_ok, _ = await self._health_monitor.is_request_allowed(
                CircuitScope.PROVIDER, cand_provider
            )
            model_ok, _ = await self._health_monitor.is_request_allowed(
                CircuitScope.MODEL, canonical_mid
            )
            if not prov_ok or not model_ok:
                attempts.append(
                    ExecutionAttemptTrace(
                        attempt_number=idx,
                        provider_id=cand_provider,
                        model_id=cand_model_id,
                        success=False,
                        error_code="GW_CIRCUIT_OPEN",
                        error_message=f"Circuit OPEN for {canonical_mid}",
                        fallback_reason=fb_reason,
                    )
                )
                last_exc = ServiceUnavailableError(f"Circuit OPEN for {canonical_mid}")
                if not allow_fallback:
                    break
                continue

            adapter = self._provider_registry.get_adapter(cand_provider)
            attempt_req = request.model_copy(
                update={
                    "requested_model": cand_model_id,
                    "provider_model_id": cand_model_id,
                },
                deep=True,
            )
            cred = (
                explicit_credential
                if cand_provider == primary_provider
                else None
            )
            start_ts = time.perf_counter()
            raw_iter = adapter.execute_stream(attempt_req, cred)
            try:
                first_event = await anext(raw_iter)
            except StopAsyncIteration:
                async def _empty_gen() -> AsyncIterator[CanonicalStreamEvent]:
                    if False:
                        yield first_event
                return _empty_gen(), cand_provider, cand_model_id, attempts
            except GatewayError as exc:
                await self._health_monitor.record_failure(
                    provider_id=cand_provider,
                    model_id=canonical_mid,
                    error_code=exc.error_code,
                    trip_circuit=not isinstance(exc, RateLimitExceededError),
                )
                attempts.append(
                    ExecutionAttemptTrace(
                        attempt_number=idx,
                        provider_id=cand_provider,
                        model_id=cand_model_id,
                        success=False,
                        error_code=exc.error_code,
                        error_message=exc.message,
                        fallback_reason=fb_reason,
                    )
                )
                last_exc = exc
                if not exc.retryable or not allow_fallback:
                    raise
                continue

            ttft_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
            attempts.append(
                ExecutionAttemptTrace(
                    attempt_number=idx,
                    provider_id=cand_provider,
                    model_id=cand_model_id,
                    success=True,
                    latency_ms=ttft_ms,
                    fallback_reason=fb_reason,
                )
            )

            async def _guarded_stream(
                first: CanonicalStreamEvent,
                rest: AsyncIterator[CanonicalStreamEvent],
                prov: str,
                mid: str,
                ttft: float,
            ) -> AsyncIterator[CanonicalStreamEvent]:
                bytes_sent = 1
                yield first
                try:
                    async for ev in rest:
                        bytes_sent += 1
                        yield ev
                    total_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
                    await self._health_monitor.record_success(
                        provider_id=prov,
                        model_id=mid,
                        latency_ms=total_ms,
                        ttft_ms=ttft,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as stream_exc:
                    # Mid-stream failure: FallbackCompatibilityGuard forbids switching after bytes_sent > 0
                    await self._health_monitor.record_failure(
                        provider_id=prov,
                        model_id=mid,
                        error_code="MID_STREAM_ERROR",
                    )
                    raise stream_exc

            return (
                _guarded_stream(first_event, raw_iter, cand_provider, canonical_mid, ttft_ms),
                cand_provider,
                cand_model_id,
                attempts,
            )

        if isinstance(last_exc, GatewayError):
            raise last_exc
        raise ServiceUnavailableError("All streaming candidates failed before first byte")

    async def _execute_single_candidate(
        self,
        *,
        adapter,
        request: CanonicalInternalRequest,
        explicit_credential: AdapterCredential | None,
        tool_mode: ToolExecutionMode,
        client_id: str | None,
        allowed_tools: set[str] | None,
        max_tool_rounds: int | None,
    ) -> tuple[CanonicalInternalResponse, str | None]:
        # Check if CredentialManager has active managed credentials for this provider when no explicit header credential is given
        if explicit_credential is None and self._credential_manager is not None:
            managed_creds = await self._credential_manager.list_credentials(
                provider_id=adapter.provider_id
            )
            if managed_creds:
                async def _op(cred: AdapterCredential) -> CanonicalInternalResponse:
                    if self._tool_orchestrator is not None:
                        return await self._tool_orchestrator.execute_with_tool_orchestration(
                            adapter=adapter,
                            request=request,
                            credential=cred,
                            mode=tool_mode,
                            client_id=client_id,
                            allowed_tools=allowed_tools,
                            max_tool_rounds=max_tool_rounds,
                        )
                    return await adapter.execute(request, cred)

                resp, lease = await self._credential_manager.execute_with_rotation(
                    provider_id=adapter.provider_id,
                    model=request.requested_model,
                    operation=_op,
                )
                return resp, lease.credential.credential_id

        if self._tool_orchestrator is not None:
            resp = await self._tool_orchestrator.execute_with_tool_orchestration(
                adapter=adapter,
                request=request,
                credential=explicit_credential,
                mode=tool_mode,
                client_id=client_id,
                allowed_tools=allowed_tools,
                max_tool_rounds=max_tool_rounds,
            )
        else:
            resp = await adapter.execute(request, explicit_credential)
        return resp, (explicit_credential.credential_id if explicit_credential else None)

    def _build_candidate_chain(
        self,
        *,
        request: CanonicalInternalRequest,
        primary_provider: str,
        primary_model: str,
        decision_id: str | None,
        allow_fallback: bool,
        allow_cross_provider: bool,
    ) -> list[tuple[str, str, str | None]]:
        chain: list[tuple[str, str, str | None]] = [
            (primary_provider, primary_model, None)
        ]
        if not allow_fallback:
            return chain

        seen: set[tuple[str, str]] = {(primary_provider, primary_model)}

        # 1. Include ranked fallback candidates from RoutingDecisionTrace if available
        if decision_id:
            try:
                trace = self._routing_engine.get_trace(decision_id)
                for fb in trace.fallback_decisions:
                    try:
                        meta = self._model_registry.get_model(fb.to_model_id)
                        key = (meta.provider_id, meta.provider_model_id)
                        if key not in seen:
                            seen.add(key)
                            chain.append((meta.provider_id, meta.provider_model_id, fb.reason))
                    except ResourceNotFoundError:
                        pass
            except ResourceNotFoundError:
                pass

        # 2. If chain still only has the primary candidate, find compatible peers in ModelRegistry
        if len(chain) == 1:
            peers: list[ModelMetadata] = []
            for m in self._model_registry.list_models(enabled_only=True):
                key = (m.provider_id, m.provider_model_id)
                if key in seen:
                    continue
                verdict = self._guard.check_compatibility(
                    request=request,
                    primary_provider_id=primary_provider,
                    candidate_model=m,
                    allow_cross_provider=allow_cross_provider,
                )
                if verdict.compatible:
                    peers.append(m)
            peers.sort(
                key=lambda m: (
                    0 if m.provider_id == primary_provider else 1,
                    -m.reliability_score,
                    m.model_id,
                )
            )
            for peer in peers[:3]:
                chain.append(
                    (
                        peer.provider_id,
                        peer.provider_model_id,
                        f"Compatible fallback peer ({peer.model_id})",
                    )
                )

        return chain

