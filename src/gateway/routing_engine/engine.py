"""Two-Stage Intelligent Routing Engine with hard capability filtering and weighted ranking."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any
import uuid

from gateway.capability_registry.models import CapabilityFlag, CapabilityState
from gateway.core.canonical import CanonicalInternalRequest, HealthStatus
from gateway.core.exceptions import NoCandidateFoundError, ResourceNotFoundError
from gateway.credential_manager.manager import CredentialManager
from gateway.model_registry.models import ModalityType, ModelMetadata
from gateway.model_registry.registry import ModelRegistry
from gateway.provider_registry.registry import ProviderRegistry
from gateway.routing_engine.models import (
    CandidateScoreBreakdown,
    FallbackDecisionRecord,
    FilteredCandidateReason,
    RouteSimulationRequest,
    RoutingDecisionTrace,
)
from gateway.routing_policy.engine import RoutingPolicyEngine
from gateway.routing_policy.models import RoutingPolicySpec


class RoutingEngine:
    """Executes two-stage capability-filtered, policy-weighted model routing."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        policy_engine: RoutingPolicyEngine,
        provider_registry: ProviderRegistry,
        credential_manager: CredentialManager | None = None,
        max_stored_traces: int = 1000,
    ) -> None:
        self._model_registry = model_registry
        self._policy_engine = policy_engine
        self._provider_registry = provider_registry
        self._credential_manager = credential_manager
        self._traces: OrderedDict[str, RoutingDecisionTrace] = OrderedDict()
        self._traces_by_request: dict[str, str] = {}
        self._max_stored_traces = max_stored_traces

    def record_trace(self, trace: RoutingDecisionTrace) -> None:
        self._traces[trace.decision_id] = trace
        self._traces_by_request[trace.request_id] = trace.decision_id
        while len(self._traces) > self._max_stored_traces:
            oldest_key, oldest_trace = self._traces.popitem(last=False)
            self._traces_by_request.pop(oldest_trace.request_id, None)

    def get_trace(self, decision_or_request_id: str) -> RoutingDecisionTrace:
        if decision_or_request_id in self._traces:
            return self._traces[decision_or_request_id]
        mapped_id = self._traces_by_request.get(decision_or_request_id)
        if mapped_id and mapped_id in self._traces:
            return self._traces[mapped_id]
        raise ResourceNotFoundError(
            f"Routing decision trace {decision_or_request_id!r} not found",
            param="decision_id",
        )

    async def route_canonical_request(
        self,
        request: CanonicalInternalRequest,
        *,
        allowed_providers_override: list[str] | None = None,
        blocked_providers_override: list[str] | None = None,
        max_cost_override: float | None = None,
    ) -> RoutingDecisionTrace:
        """Routes a CanonicalInternalRequest through Stage 1 and Stage 2 and returns a RoutingDecisionTrace."""
        input_mods: set[ModalityType] = {ModalityType.TEXT}
        for msg in request.messages:
            for part in msg.parts:
                if part.type in ("image_url", "image_base64"):
                    input_mods.add(ModalityType.IMAGE)
                elif part.type == "audio":
                    input_mods.add(ModalityType.AUDIO)

        output_mods: set[ModalityType] = {ModalityType.TEXT}
        if CapabilityFlag.EMBEDDINGS in request.required_capabilities:
            output_mods = {ModalityType.EMBEDDING}
        elif CapabilityFlag.TTS in request.required_capabilities:
            output_mods = {ModalityType.AUDIO}

        est_tokens = request.estimated_input_tokens
        if est_tokens == 0:
            char_sum = sum(len(s) for s in request.system_instructions) + sum(
                len(m.combined_text()) for m in request.messages
            )
            est_tokens = max(char_sum // 4, 1) if char_sum > 0 else 0

        sim_req = RouteSimulationRequest(
            model=request.requested_model,
            task=request.task_hint,
            policy=request.routing_policy_name,
            required_capabilities=sorted(
                request.required_capabilities, key=lambda c: c.value
            ),
            input_modalities=sorted(input_mods, key=lambda m: m.value),
            output_modalities=sorted(output_mods, key=lambda m: m.value),
            estimated_input_tokens=est_tokens,
            max_output_tokens=request.max_output_tokens,
            stream=request.stream,
            tools_count=len(request.tools),
            structured_output=(
                request.response_format is not None
                and request.response_format.type == "json_schema"
            ),
            allowed_providers=allowed_providers_override,
            blocked_providers=blocked_providers_override or [],
            max_cost_per_1m_usd=max_cost_override,
        )
        return await self.evaluate_route(sim_req, request_id=request.request_id)

    async def evaluate_route(
        self,
        req: RouteSimulationRequest,
        *,
        request_id: str | None = None,
    ) -> RoutingDecisionTrace:
        """Executes Stage 1 (Capability Filter) and Stage 2 (Weighted Ranking) deterministically."""
        req_id = request_id or str(uuid.uuid4())
        requested_target = req.model.strip()

        required_caps: set[CapabilityFlag] = set(req.required_capabilities)
        if req.stream:
            required_caps.add(CapabilityFlag.STREAMING)
        if req.tools_count > 0:
            required_caps.add(CapabilityFlag.TOOL_CALLING)
        if req.structured_output:
            required_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)

        required_in_mods = set(req.input_modalities)
        required_out_mods = set(req.output_modalities)
        if ModalityType.IMAGE in required_in_mods:
            required_caps.add(CapabilityFlag.VISION)

        # Resolve Alias vs Explicit Model
        if self._model_registry.is_alias(requested_target):
            alias_spec = self._model_registry.get_alias(requested_target)
            task_type = req.task or alias_spec.task_type
            policy_name = req.policy or alias_spec.default_policy
            required_caps.update(alias_spec.required_capabilities)
            required_in_mods.update(alias_spec.required_input_modalities)
            required_out_mods = set(alias_spec.required_output_modalities)
            if alias_spec.candidate_model_ids:
                initial_candidates = [
                    self._model_registry.get_model(mid)
                    for mid in alias_spec.candidate_model_ids
                ]
            else:
                initial_candidates = self._model_registry.list_models(enabled_only=False)
        else:
            task_type = req.task or "general"
            policy_name = req.policy or "balanced"
            # If explicit model is requested, check if it's in the registry, plus compatible fallback peers if allowed
            try:
                primary_model = self._model_registry.get_model(requested_target)
                initial_candidates = [primary_model]
            except ResourceNotFoundError:
                initial_candidates = self._model_registry.list_models(enabled_only=False)

        policy: RoutingPolicySpec = self._policy_engine.get_policy(policy_name)
        allowed_providers = (
            set(req.allowed_providers)
            if req.allowed_providers is not None
            else (
                set(policy.allowed_providers)
                if policy.allowed_providers is not None
                else None
            )
        )
        blocked_providers = set(policy.blocked_providers) | set(req.blocked_providers)
        max_cost = (
            req.max_cost_per_1m_usd
            if req.max_cost_per_1m_usd is not None
            else policy.max_cost_per_1m_usd
        )
        required_context = req.estimated_input_tokens + (req.max_output_tokens or 0)

        # =====================================================================
        # STAGE 1: Hard Capability & Constraint Filtering
        # =====================================================================
        surviving: list[ModelMetadata] = []
        filtered_reasons: list[FilteredCandidateReason] = []

        for cand in initial_candidates:
            reason = self._check_stage1_candidate(
                cand=cand,
                required_caps=required_caps,
                required_in_mods=required_in_mods,
                required_out_mods=required_out_mods,
                required_context=required_context,
                allowed_providers=allowed_providers,
                blocked_providers=blocked_providers,
                max_cost=max_cost,
                max_latency_p95_ms=policy.max_latency_p95_ms,
                min_reliability=policy.min_reliability_score,
            )
            if reason is not None:
                filtered_reasons.append(reason)
            else:
                surviving.append(cand)

        if not surviving:
            trace = RoutingDecisionTrace(
                request_id=req_id,
                original_request=req.model_dump(),
                requested_model_or_alias=requested_target,
                requested_task=task_type,
                applied_policy=policy.name,
                weights=policy.weights,
                required_capabilities=sorted(required_caps, key=lambda c: c.value),
                required_input_modalities=sorted(required_in_mods, key=lambda m: m.value),
                required_output_modalities=sorted(required_out_mods, key=lambda m: m.value),
                required_context_length=required_context,
                candidate_models=[c.model_id for c in initial_candidates],
                filtered_candidates=filtered_reasons,
                ranking_scores=[],
                selected_model="",
                selected_provider="",
            )
            self.record_trace(trace)
            raise NoCandidateFoundError(
                f"No candidate models satisfied Stage 1 hard filtering for {requested_target!r}",
                param="model",
                details={
                    "decision_id": trace.decision_id,
                    "filtered_count": len(filtered_reasons),
                    "filtered_reasons": [r.model_dump() for r in filtered_reasons[:10]],
                },
            )

        # =====================================================================
        # STAGE 2: Weighted Candidate Ranking
        # =====================================================================
        capacity_by_provider = await self._compute_provider_capacity_ratios(
            {c.provider_id for c in surviving}
        )
        ranking_scores = self._rank_stage2_candidates(
            candidates=surviving,
            task_type=task_type,
            policy=policy,
            capacity_by_provider=capacity_by_provider,
        )

        winner = ranking_scores[0]
        cred_ref = await self._resolve_safe_credential_ref(winner.provider_id)

        fallback_decisions: list[FallbackDecisionRecord] = []
        for idx, runner_up in enumerate(ranking_scores[1:4], start=1):
            if (
                not policy.allow_cross_provider_fallback
                and runner_up.provider_id != winner.provider_id
            ):
                continue
            fallback_decisions.append(
                FallbackDecisionRecord(
                    step=idx,
                    from_model_id=ranking_scores[idx - 1].model_id,
                    to_model_id=runner_up.model_id,
                    to_provider_id=runner_up.provider_id,
                    reason=f"Ranked #{idx + 1} compatible candidate (score={runner_up.total_score:.4f})",
                )
            )

        trace = RoutingDecisionTrace(
            request_id=req_id,
            original_request=req.model_dump(),
            requested_model_or_alias=requested_target,
            requested_task=task_type,
            applied_policy=policy.name,
            weights=policy.weights,
            required_capabilities=sorted(required_caps, key=lambda c: c.value),
            required_input_modalities=sorted(required_in_mods, key=lambda m: m.value),
            required_output_modalities=sorted(required_out_mods, key=lambda m: m.value),
            required_context_length=required_context,
            candidate_models=[c.model_id for c in initial_candidates],
            filtered_candidates=filtered_reasons,
            ranking_scores=ranking_scores,
            selected_model=winner.model_id,
            selected_provider=winner.provider_id,
            selected_credential_ref=cred_ref,
            fallback_decisions=fallback_decisions,
        )
        self.record_trace(trace)
        return trace

    def _check_stage1_candidate(
        self,
        *,
        cand: ModelMetadata,
        required_caps: set[CapabilityFlag],
        required_in_mods: set[ModalityType],
        required_out_mods: set[ModalityType],
        required_context: int,
        allowed_providers: set[str] | None,
        blocked_providers: set[str],
        max_cost: float | None,
        max_latency_p95_ms: float | None,
        min_reliability: float | None,
    ) -> FilteredCandidateReason | None:
        if not cand.enabled:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="MODEL_DISABLED",
                details="Model is disabled in registry",
            )

        if self._provider_registry.has_provider(cand.provider_id):
            adapter = self._provider_registry.get_adapter(cand.provider_id)
            if not adapter.config.enabled:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="PROVIDER_DISABLED",
                    details=f"Provider {cand.provider_id!r} is disabled",
                )

        if cand.health_status == HealthStatus.UNHEALTHY:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="MODEL_UNHEALTHY",
                details="Model health status is UNHEALTHY",
            )

        if allowed_providers is not None and cand.provider_id not in allowed_providers:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="PROVIDER_NOT_ALLOWED",
                details=f"Provider {cand.provider_id!r} not in allowed_providers",
            )

        if cand.provider_id in blocked_providers:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="PROVIDER_BLOCKED",
                details=f"Provider {cand.provider_id!r} is in blocked_providers",
            )

        cand_in_mods = set(cand.input_modalities)
        missing_in = required_in_mods - cand_in_mods
        if missing_in:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="UNSUPPORTED_INPUT_MODALITY",
                details=f"Missing input modalities: {sorted(m.value for m in missing_in)}",
            )

        cand_out_mods = set(cand.output_modalities)
        missing_out = required_out_mods - cand_out_mods
        if missing_out:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="UNSUPPORTED_OUTPUT_MODALITY",
                details=f"Missing output modalities: {sorted(m.value for m in missing_out)}",
            )

        # Check every required capability strictly (UNKNOWN and UNSUPPORTED both fail Stage 1)
        for cap in sorted(required_caps, key=lambda c: c.value):
            state = cand.get_capability_state(cap)
            if state == CapabilityState.UNSUPPORTED:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="UNSUPPORTED_CAPABILITY",
                    details=f"Capability {cap.value!r} is explicitly UNSUPPORTED",
                )
            if state == CapabilityState.UNKNOWN:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="UNKNOWN_CAPABILITY",
                    details=f"Capability {cap.value!r} is UNKNOWN (unverified)",
                )

        if required_context > 0:
            if cand.context_length is None:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="UNKNOWN_CONTEXT_LENGTH",
                    details=f"Model context_length is UNKNOWN (required={required_context})",
                )
            if cand.context_length < required_context:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="INSUFFICIENT_CONTEXT_LENGTH",
                    details=f"Model context_length ({cand.context_length}) < required ({required_context})",
                )

        if max_cost is not None:
            blended = cand.cost.blended_cost_per_1m_usd()
            if blended is None:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="UNKNOWN_COST",
                    details="Model cost metadata is UNKNOWN when max_cost_per_1m_usd ceiling is enforced",
                )
            if blended > max_cost:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="COST_EXCEEDS_MAX",
                    details=f"Blended cost ${blended:.4f}/1M exceeds ceiling ${max_cost:.4f}/1M",
                )

        if max_latency_p95_ms is not None and cand.latency.p95_ms is not None:
            if cand.latency.p95_ms > max_latency_p95_ms:
                return FilteredCandidateReason(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    reason_code="LATENCY_EXCEEDS_MAX",
                    details=f"P95 latency {cand.latency.p95_ms}ms exceeds ceiling {max_latency_p95_ms}ms",
                )

        if min_reliability is not None and cand.reliability_score < min_reliability:
            return FilteredCandidateReason(
                model_id=cand.model_id,
                provider_id=cand.provider_id,
                reason_code="RELIABILITY_BELOW_MIN",
                details=f"Reliability {cand.reliability_score:.3f} < minimum {min_reliability:.3f}",
            )

        return None

    def _rank_stage2_candidates(
        self,
        *,
        candidates: list[ModelMetadata],
        task_type: str,
        policy: RoutingPolicySpec,
        capacity_by_provider: dict[str, float],
    ) -> list[CandidateScoreBreakdown]:
        known_costs = [
            c.cost.blended_cost_per_1m_usd()
            for c in candidates
            if c.cost.blended_cost_per_1m_usd() is not None
        ]
        min_cost = min(known_costs) if known_costs else 0.0
        max_cost = max(known_costs) if known_costs else 1.0

        known_latencies = [
            c.latency.p50_ms for c in candidates if c.latency.p50_ms is not None
        ]
        min_lat = min(known_latencies) if known_latencies else 100.0
        max_lat = max(known_latencies) if known_latencies else 1000.0

        w = policy.weights
        breakdowns: list[CandidateScoreBreakdown] = []

        for cand in candidates:
            q_score = cand.task_quality_score(task_type)
            health_factor = 0.6 if cand.health_status == HealthStatus.DEGRADED else 1.0
            r_score = round(cand.reliability_score * health_factor, 4)

            blended = cand.cost.blended_cost_per_1m_usd()
            if blended is None:
                c_score = 0.5
            elif abs(max_cost - min_cost) < 1e-6:
                c_score = 1.0
            else:
                c_score = round((max_cost - blended) / (max_cost - min_cost), 4)

            p50 = cand.latency.p50_ms
            if p50 is None:
                l_score = 0.5
            elif abs(max_lat - min_lat) < 1e-6:
                l_score = 1.0
            else:
                l_score = round((max_lat - p50) / (max_lat - min_lat), 4)

            cap_score = round(capacity_by_provider.get(cand.provider_id, 1.0), 4)

            contrib = {
                "quality": round(w.quality * q_score, 4),
                "reliability": round(w.reliability * r_score, 4),
                "cost": round(w.cost * c_score, 4),
                "latency": round(w.latency * l_score, 4),
                "capacity": round(w.capacity * cap_score, 4),
            }
            total = round(sum(contrib.values()), 4)
            breakdowns.append(
                CandidateScoreBreakdown(
                    model_id=cand.model_id,
                    provider_id=cand.provider_id,
                    total_score=total,
                    quality_score=round(q_score, 4),
                    reliability_score=r_score,
                    cost_score=c_score,
                    latency_score=l_score,
                    capacity_score=cap_score,
                    weighted_contributions=contrib,
                )
            )

        # Deterministic sorting: highest total_score, highest quality_score, highest cost_score, then alphabetical model_id
        breakdowns.sort(
            key=lambda b: (
                -b.total_score,
                -b.quality_score,
                -b.cost_score,
                b.model_id,
            )
        )
        return breakdowns

    async def _compute_provider_capacity_ratios(
        self, provider_ids: set[str]
    ) -> dict[str, float]:
        if self._credential_manager is None:
            return {pid: 1.0 for pid in provider_ids}

        ratios: dict[str, float] = {}
        for pid in provider_ids:
            creds = await self._credential_manager.list_credentials(provider_id=pid)
            if not creds:
                ratios[pid] = 1.0
                continue
            total_slots = 0
            avail_slots = 0
            for c in creds:
                health = await self._credential_manager.get_credential_health(c.id)
                slots = c.max_concurrency or 100
                total_slots += slots
                if health.is_eligible:
                    avail_slots += max(slots - health.active_requests, 0)
            ratios[pid] = avail_slots / max(total_slots, 1)
        return ratios

    async def _resolve_safe_credential_ref(self, provider_id: str) -> str | None:
        """Returns a non-sensitive credential reference string (never exposing raw secret values)."""
        if self._credential_manager is None:
            return f"provider:{provider_id}:unmanaged"
        creds = await self._credential_manager.list_credentials(
            provider_id=provider_id
        )
        for c in creds:
            health = await self._credential_manager.get_credential_health(c.id)
            if health.is_eligible:
                return f"cred:{c.id} ({c.label}, masked={c.masked_preview})"
        return f"provider:{provider_id}:default"

