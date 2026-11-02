"""FallbackCompatibilityGuard enforcing stream safety, idempotency, and mandatory capability parity."""

from __future__ import annotations

from gateway.capability_registry.models import CapabilityFlag, CapabilityState
from gateway.core.canonical import CanonicalInternalRequest, HealthStatus
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.model_registry.models import ModalityType, ModelMetadata
from gateway.retry_fallback.models import FallbackGuardVerdict
from gateway.tool_gateway.registry import ToolRegistry


class FallbackCompatibilityGuard:
    """Verifies that switching from a failed candidate to a fallback candidate is strictly safe."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._tool_registry = tool_registry

    def check_compatibility(
        self,
        *,
        request: CanonicalInternalRequest,
        primary_provider_id: str,
        candidate_model: ModelMetadata,
        bytes_sent_to_client: int = 0,
        non_idempotent_tool_executed: bool = False,
        allow_cross_provider: bool = True,
    ) -> FallbackGuardVerdict:
        """Evaluates all safety invariants before permitting fallback to candidate_model."""
        # 1. Stream State Invariant: Never fallback once any byte/chunk has been streamed to the client
        if bytes_sent_to_client > 0:
            return FallbackGuardVerdict(
                compatible=False,
                reason_code="STREAM_ALREADY_STARTED",
                details=(
                    f"Cannot fallback to {candidate_model.model_id!r} because "
                    f"{bytes_sent_to_client} bytes have already been streamed to client"
                ),
            )

        # 2. Tool Idempotency Invariant: Never replay if a non-idempotent tool executed with side effects
        if non_idempotent_tool_executed:
            return FallbackGuardVerdict(
                compatible=False,
                reason_code="NON_IDEMPOTENT_TOOL_EXECUTED",
                details="Cannot fallback after a non-idempotent tool call has been executed",
            )

        # 3. Model Health & Enabled Check
        if not candidate_model.enabled or candidate_model.health_status == HealthStatus.UNHEALTHY:
            return FallbackGuardVerdict(
                compatible=False,
                reason_code="CANDIDATE_UNHEALTHY_OR_DISABLED",
                details=f"Fallback candidate {candidate_model.model_id!r} is disabled or UNHEALTHY",
            )

        # 4. Cross-Provider & Native Mode Check
        if candidate_model.provider_id != primary_provider_id:
            if request.integration_mode == "native":
                return FallbackGuardVerdict(
                    compatible=False,
                    reason_code="NATIVE_MODE_CROSS_PROVIDER_FORBIDDEN",
                    details=(
                        f"Cannot fallback from native provider {primary_provider_id!r} "
                        f"to {candidate_model.provider_id!r} in native passthrough mode"
                    ),
                )
            if not allow_cross_provider:
                return FallbackGuardVerdict(
                    compatible=False,
                    reason_code="CROSS_PROVIDER_DISABLED_BY_POLICY",
                    details="Policy forbids cross-provider fallback",
                )

        # 5. Provider-Native Tool Non-Portability Check
        if self._tool_registry is not None and request.tools:
            try:
                self._tool_registry.enforce_provider_native_portability(
                    request.tools, candidate_model.provider_id
                )
            except UnsupportedCapabilityError as exc:
                return FallbackGuardVerdict(
                    compatible=False,
                    reason_code="PROVIDER_NATIVE_TOOL_INCOMPATIBLE",
                    details=exc.message,
                )

        # 6. Mandatory Modality Parity
        required_in_mods: set[ModalityType] = {ModalityType.TEXT}
        for msg in request.messages:
            for part in msg.parts:
                if part.type in ("image_url", "image_base64"):
                    required_in_mods.add(ModalityType.IMAGE)
                elif part.type == "audio":
                    required_in_mods.add(ModalityType.AUDIO)
        missing_in = required_in_mods - set(candidate_model.input_modalities)
        if missing_in:
            return FallbackGuardVerdict(
                compatible=False,
                reason_code="MISSING_INPUT_MODALITY",
                details=f"Fallback model missing input modalities: {sorted(m.value for m in missing_in)}",
            )

        # 7. Mandatory Capability Parity (UNKNOWN and UNSUPPORTED are both rejected)
        required_caps = set(request.required_capabilities)
        if request.stream:
            required_caps.add(CapabilityFlag.STREAMING)
        if request.tools:
            required_caps.add(CapabilityFlag.TOOL_CALLING)
        if (
            request.response_format is not None
            and request.response_format.type == "json_schema"
        ):
            required_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)

        for cap in sorted(required_caps, key=lambda c: c.value):
            state = candidate_model.get_capability_state(cap)
            if state != CapabilityState.SUPPORTED:
                return FallbackGuardVerdict(
                    compatible=False,
                    reason_code="MISSING_REQUIRED_CAPABILITY",
                    details=(
                        f"Fallback candidate {candidate_model.model_id!r} capability "
                        f"{cap.value!r} is {state.value}"
                    ),
                )

        # 8. Context Window Parity
        est_tokens = request.estimated_input_tokens
        if est_tokens == 0:
            char_sum = sum(len(s) for s in request.system_instructions) + sum(
                len(m.combined_text()) for m in request.messages
            )
            est_tokens = max(char_sum // 4, 1) if char_sum > 0 else 0
        required_ctx = est_tokens + (request.max_output_tokens or 0)
        if required_ctx > 0:
            if (
                candidate_model.context_length is None
                or candidate_model.context_length < required_ctx
            ):
                return FallbackGuardVerdict(
                    compatible=False,
                    reason_code="INSUFFICIENT_CONTEXT_WINDOW",
                    details=(
                        f"Fallback candidate context_length ({candidate_model.context_length}) "
                        f"< required ({required_ctx})"
                    ),
                )

        return FallbackGuardVerdict(compatible=True, reason_code="OK")

