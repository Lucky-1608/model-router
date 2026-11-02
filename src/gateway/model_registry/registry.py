"""Central Model Registry managing model metadata, provider discovery sync, and semantic aliases."""

from __future__ import annotations

from datetime import datetime, timezone

from gateway.capability_registry.models import (
    CapabilityFlag,
    CapabilityState,
    ProtocolType,
    build_capability_map,
)
from gateway.core.canonical import DiscoveredModel, HealthStatus
from gateway.core.exceptions import ResourceNotFoundError
from gateway.model_registry.models import (
    CostMetadata,
    DiscoverySource,
    LatencyStatistics,
    ModalityType,
    ModelAliasSpec,
    ModelMetadata,
    ModelRegistrationRequest,
)


class ModelRegistry:
    """Central registry for AI models, capabilities, pricing, latency stats, and semantic aliases."""

    def __init__(self, *, seed_defaults: bool = True) -> None:
        self._models: dict[str, ModelMetadata] = {}
        self._aliases: dict[str, ModelAliasSpec] = {}
        if seed_defaults:
            self._seed_verified_models()
            self._seed_default_aliases()

    def _seed_verified_models(self) -> None:
        """Seeds verified models from official provider specifications."""
        seeded: list[ModelMetadata] = [
            ModelMetadata(
                model_id="openai/gpt-4o",
                provider_id="openai",
                provider_model_id="gpt-4o",
                display_name="GPT-4o",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.OPENAI_CHAT,
                    ProtocolType.OPENAI_RESPONSES,
                ],
                input_modalities=[ModalityType.TEXT, ModalityType.IMAGE],
                output_modalities=[ModalityType.TEXT],
                context_length=128000,
                max_output_tokens=16384,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.PARALLEL_TOOL_CALLS,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.VISION,
                    },
                    unsupported={
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=2.50,
                    output_cost_per_1m_usd=10.00,
                    cached_input_cost_per_1m_usd=1.25,
                ),
                latency=LatencyStatistics(
                    p50_ms=620.0, p95_ms=1350.0, ttft_ms=240.0, sample_count=100
                ),
                reliability_score=0.992,
                quality_scores={
                    "general": 0.94,
                    "auto": 0.94,
                    "balanced": 0.94,
                    "coding": 0.92,
                    "reasoning": 0.90,
                    "fast": 0.80,
                    "classification": 0.93,
                    "summarization": 0.94,
                    "vision": 0.96,
                },
            ),
            ModelMetadata(
                model_id="openai/gpt-4o-mini",
                provider_id="openai",
                provider_model_id="gpt-4o-mini",
                display_name="GPT-4o Mini",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.OPENAI_CHAT,
                    ProtocolType.OPENAI_RESPONSES,
                ],
                input_modalities=[ModalityType.TEXT, ModalityType.IMAGE],
                output_modalities=[ModalityType.TEXT],
                context_length=128000,
                max_output_tokens=16384,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.PARALLEL_TOOL_CALLS,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.VISION,
                    },
                    unsupported={
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=0.15,
                    output_cost_per_1m_usd=0.60,
                    cached_input_cost_per_1m_usd=0.075,
                ),
                latency=LatencyStatistics(
                    p50_ms=340.0, p95_ms=720.0, ttft_ms=130.0, sample_count=100
                ),
                reliability_score=0.995,
                quality_scores={
                    "general": 0.82,
                    "auto": 0.84,
                    "balanced": 0.86,
                    "coding": 0.80,
                    "reasoning": 0.76,
                    "fast": 0.92,
                    "classification": 0.91,
                    "summarization": 0.89,
                    "vision": 0.85,
                },
            ),
            ModelMetadata(
                model_id="openai/o3-mini",
                provider_id="openai",
                provider_model_id="o3-mini",
                display_name="OpenAI o3-mini",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.OPENAI_CHAT,
                    ProtocolType.OPENAI_RESPONSES,
                ],
                input_modalities=[ModalityType.TEXT],
                output_modalities=[ModalityType.TEXT],
                context_length=200000,
                max_output_tokens=100000,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.REASONING_TOKENS,
                    },
                    unsupported={
                        CapabilityFlag.VISION,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=1.10,
                    output_cost_per_1m_usd=4.40,
                    cached_input_cost_per_1m_usd=0.55,
                ),
                latency=LatencyStatistics(
                    p50_ms=1450.0, p95_ms=3200.0, ttft_ms=650.0, sample_count=100
                ),
                reliability_score=0.990,
                quality_scores={
                    "general": 0.92,
                    "coding": 0.95,
                    "reasoning": 0.98,
                    "balanced": 0.88,
                    "fast": 0.60,
                },
            ),
            ModelMetadata(
                model_id="openai/text-embedding-3-small",
                provider_id="openai",
                provider_model_id="text-embedding-3-small",
                display_name="Text Embedding 3 Small",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[ProtocolType.OPENAI_EMBEDDINGS],
                input_modalities=[ModalityType.TEXT],
                output_modalities=[ModalityType.EMBEDDING],
                context_length=8191,
                capabilities=build_capability_map(
                    supported={CapabilityFlag.EMBEDDINGS},
                    unsupported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.VISION,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=0.02,
                    output_cost_per_1m_usd=0.0,
                ),
                latency=LatencyStatistics(
                    p50_ms=110.0, p95_ms=240.0, sample_count=100
                ),
                reliability_score=0.998,
                quality_scores={"embedding": 0.94, "general": 0.90},
            ),
            ModelMetadata(
                model_id="anthropic/claude-3-7-sonnet-20250219",
                provider_id="anthropic",
                provider_model_id="claude-3-7-sonnet-20250219",
                display_name="Claude 3.7 Sonnet",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.ANTHROPIC_MESSAGES,
                    ProtocolType.OPENAI_CHAT,
                ],
                input_modalities=[ModalityType.TEXT, ModalityType.IMAGE],
                output_modalities=[ModalityType.TEXT],
                context_length=200000,
                max_output_tokens=64000,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.MESSAGES_API,
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.PARALLEL_TOOL_CALLS,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.REASONING_TOKENS,
                        CapabilityFlag.VISION,
                    },
                    unsupported={
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=3.00,
                    output_cost_per_1m_usd=15.00,
                    cached_input_cost_per_1m_usd=0.30,
                ),
                latency=LatencyStatistics(
                    p50_ms=710.0, p95_ms=1550.0, ttft_ms=280.0, sample_count=100
                ),
                reliability_score=0.994,
                quality_scores={
                    "general": 0.97,
                    "auto": 0.96,
                    "balanced": 0.95,
                    "coding": 0.99,
                    "reasoning": 0.97,
                    "fast": 0.78,
                    "classification": 0.94,
                    "summarization": 0.96,
                    "vision": 0.95,
                },
            ),
            ModelMetadata(
                model_id="gemini/gemini-2.5-pro",
                provider_id="gemini",
                provider_model_id="gemini-2.5-pro",
                display_name="Gemini 2.5 Pro",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.GEMINI_GENERATE_CONTENT,
                    ProtocolType.OPENAI_CHAT,
                ],
                input_modalities=[
                    ModalityType.TEXT,
                    ModalityType.IMAGE,
                    ModalityType.AUDIO,
                ],
                output_modalities=[ModalityType.TEXT],
                context_length=1048576,
                max_output_tokens=65536,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.GEMINI_GENERATE_CONTENT,
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.PARALLEL_TOOL_CALLS,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.REASONING_TOKENS,
                        CapabilityFlag.VISION,
                        CapabilityFlag.AUDIO_INPUT,
                    },
                    unsupported={
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.MESSAGES_API,
                        CapabilityFlag.TTS,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=1.25,
                    output_cost_per_1m_usd=10.00,
                ),
                latency=LatencyStatistics(
                    p50_ms=780.0, p95_ms=1700.0, ttft_ms=310.0, sample_count=100
                ),
                reliability_score=0.991,
                quality_scores={
                    "general": 0.96,
                    "auto": 0.95,
                    "balanced": 0.94,
                    "coding": 0.96,
                    "reasoning": 0.97,
                    "summarization": 0.97,
                    "vision": 0.98,
                },
            ),
            ModelMetadata(
                model_id="gemini/gemini-2.0-flash",
                provider_id="gemini",
                provider_model_id="gemini-2.0-flash",
                display_name="Gemini 2.0 Flash",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[
                    ProtocolType.GEMINI_GENERATE_CONTENT,
                    ProtocolType.OPENAI_CHAT,
                ],
                input_modalities=[
                    ModalityType.TEXT,
                    ModalityType.IMAGE,
                    ModalityType.AUDIO,
                ],
                output_modalities=[ModalityType.TEXT],
                context_length=1048576,
                max_output_tokens=8192,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.GEMINI_GENERATE_CONTENT,
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.VISION,
                        CapabilityFlag.AUDIO_INPUT,
                    },
                    unsupported={
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.MESSAGES_API,
                        CapabilityFlag.TTS,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=0.10,
                    output_cost_per_1m_usd=0.40,
                ),
                latency=LatencyStatistics(
                    p50_ms=250.0, p95_ms=520.0, ttft_ms=100.0, sample_count=100
                ),
                reliability_score=0.993,
                quality_scores={
                    "general": 0.86,
                    "auto": 0.88,
                    "balanced": 0.90,
                    "coding": 0.82,
                    "reasoning": 0.80,
                    "fast": 0.96,
                    "classification": 0.92,
                    "summarization": 0.92,
                    "vision": 0.91,
                },
            ),
            ModelMetadata(
                model_id="groq/llama-3.3-70b-versatile",
                provider_id="groq",
                provider_model_id="llama-3.3-70b-versatile",
                display_name="Llama 3.3 70B Versatile (Groq)",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[ProtocolType.OPENAI_CHAT],
                input_modalities=[ModalityType.TEXT],
                output_modalities=[ModalityType.TEXT],
                context_length=128000,
                max_output_tokens=32768,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                    },
                    unsupported={
                        CapabilityFlag.VISION,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.RESPONSES_API,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=0.59,
                    output_cost_per_1m_usd=0.79,
                ),
                latency=LatencyStatistics(
                    p50_ms=145.0, p95_ms=290.0, ttft_ms=55.0, sample_count=100
                ),
                reliability_score=0.988,
                quality_scores={
                    "general": 0.88,
                    "auto": 0.87,
                    "balanced": 0.90,
                    "fast": 0.99,
                    "coding": 0.86,
                    "classification": 0.90,
                    "summarization": 0.90,
                },
            ),
            ModelMetadata(
                model_id="ollama/llama3.2",
                provider_id="ollama",
                provider_model_id="llama3.2",
                display_name="Ollama Llama 3.2 (Local)",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[ProtocolType.OPENAI_CHAT],
                input_modalities=[ModalityType.TEXT],
                output_modalities=[ModalityType.TEXT],
                context_length=128000,
                max_output_tokens=8192,
                capabilities=build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                    },
                    unsupported={
                        CapabilityFlag.VISION,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                ),
                cost=CostMetadata(
                    input_cost_per_1m_usd=0.0,
                    output_cost_per_1m_usd=0.0,
                ),
                latency=LatencyStatistics(
                    p50_ms=420.0, p95_ms=890.0, ttft_ms=150.0, sample_count=50
                ),
                reliability_score=0.975,
                quality_scores={
                    "general": 0.78,
                    "auto": 0.78,
                    "balanced": 0.80,
                    "coding": 0.75,
                    "fast": 0.82,
                    "classification": 0.84,
                    "summarization": 0.82,
                },
            ),
            ModelMetadata(
                model_id="elevenlabs/eleven_multilingual_v2",
                provider_id="elevenlabs",
                provider_model_id="eleven_multilingual_v2",
                display_name="Eleven Multilingual v2 (TTS)",
                discovery_source=DiscoverySource.SEEDED_MANIFEST,
                supported_protocols=[ProtocolType.ELEVENLABS_TTS],
                input_modalities=[ModalityType.TEXT],
                output_modalities=[ModalityType.AUDIO],
                context_length=10000,
                capabilities=build_capability_map(
                    supported={CapabilityFlag.TTS, CapabilityFlag.STREAMING},
                    unsupported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TOOL_CALLING,
                    },
                ),
                cost=CostMetadata(per_character_usd=0.00018),
                latency=LatencyStatistics(
                    p50_ms=320.0, p95_ms=650.0, sample_count=50
                ),
                reliability_score=0.994,
                quality_scores={"speech": 0.98},
            ),
        ]
        for m in seeded:
            self._models[m.model_id] = m

    def _seed_default_aliases(self) -> None:
        """Initializes all 10 required semantic model aliases."""
        aliases: list[ModelAliasSpec] = [
            ModelAliasSpec(
                alias="auto",
                description="Automatic balanced selection across general-purpose chat models",
                task_type="auto",
                default_policy="balanced",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="fast",
                description="Ultra-low-latency model selection for real-time interactive tasks",
                task_type="fast",
                default_policy="latency_optimized",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="coding",
                description="High-accuracy software engineering, code generation, and debugging",
                task_type="coding",
                default_policy="quality_first",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="reasoning",
                description="Deep multi-step analytical and mathematical reasoning",
                task_type="reasoning",
                default_policy="quality_first",
                required_capabilities=[
                    CapabilityFlag.CHAT_COMPLETIONS,
                    CapabilityFlag.REASONING_TOKENS,
                ],
            ),
            ModelAliasSpec(
                alias="balanced",
                description="Balanced trade-off between quality, reliability, cost, and latency",
                task_type="balanced",
                default_policy="balanced",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="classification",
                description="Cost- and speed-effective structured classification and tagging",
                task_type="classification",
                default_policy="cost_optimized",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="summarization",
                description="Long-context document and conversation summarization",
                task_type="summarization",
                default_policy="balanced",
                required_capabilities=[CapabilityFlag.CHAT_COMPLETIONS],
            ),
            ModelAliasSpec(
                alias="speech",
                description="Text-to-speech audio synthesis",
                task_type="speech",
                default_policy="quality_first",
                required_capabilities=[CapabilityFlag.TTS],
                required_input_modalities=[ModalityType.TEXT],
                required_output_modalities=[ModalityType.AUDIO],
            ),
            ModelAliasSpec(
                alias="embedding",
                description="Dense vector embeddings for semantic search and retrieval",
                task_type="embedding",
                default_policy="cost_optimized",
                required_capabilities=[CapabilityFlag.EMBEDDINGS],
                required_input_modalities=[ModalityType.TEXT],
                required_output_modalities=[ModalityType.EMBEDDING],
            ),
            ModelAliasSpec(
                alias="vision",
                description="Multimodal image and visual document understanding",
                task_type="vision",
                default_policy="quality_first",
                required_capabilities=[
                    CapabilityFlag.CHAT_COMPLETIONS,
                    CapabilityFlag.VISION,
                ],
                required_input_modalities=[ModalityType.TEXT, ModalityType.IMAGE],
                required_output_modalities=[ModalityType.TEXT],
            ),
        ]
        for a in aliases:
            self._aliases[a.alias] = a

    # =========================================================================
    # Model Registration & Discovery Operations
    # =========================================================================
    def register_model(self, req: ModelRegistrationRequest) -> ModelMetadata:
        """Manually registers or updates a model, marking any omitted capability as UNKNOWN."""
        canonical_id = req.model_id or f"{req.provider_id}/{req.provider_model_id}"
        caps = build_capability_map(
            supported=set(req.supported_capabilities),
            unsupported=set(req.unsupported_capabilities),
        )
        for flag, state in req.capabilities.items():
            caps[flag] = state
        metadata = ModelMetadata(
            model_id=canonical_id,
            provider_id=req.provider_id,
            provider_model_id=req.provider_model_id,
            display_name=req.display_name or req.provider_model_id,
            discovery_source=DiscoverySource.MANUAL,
            supported_protocols=req.supported_protocols,
            input_modalities=req.input_modalities,
            output_modalities=req.output_modalities,
            context_length=req.context_length,
            max_output_tokens=req.max_output_tokens,
            capabilities=caps,
            cost=req.cost,
            latency=req.latency,
            reliability_score=req.reliability_score,
            quality_scores=req.quality_scores,
            health_status=req.health_status,
            enabled=req.enabled,
            updated_at=datetime.now(timezone.utc),
        )
        self._models[canonical_id] = metadata
        return metadata

    def register_manual_model(self, req: ModelRegistrationRequest) -> ModelMetadata:
        """Alias for register_model."""
        return self.register_model(req)

    def sync_discovered_models(
        self, discovered: list[DiscoveredModel]
    ) -> list[ModelMetadata]:
        """Merges provider-discovered models into the registry without inventing missing metadata."""
        synced: list[ModelMetadata] = []
        for dm in discovered:
            existing = self._models.get(dm.canonical_model_id)
            if existing is not None:
                # Preserve existing verified capabilities/pricing if discovery returned None/UNKNOWN
                merged_caps = dict(existing.capabilities)
                for flag, state in dm.capabilities.items():
                    if state != CapabilityState.UNKNOWN:
                        merged_caps[flag] = state
                updated = existing.model_copy(
                    update={
                        "context_length": dm.context_length
                        if dm.context_length is not None
                        else existing.context_length,
                        "max_output_tokens": dm.max_output_tokens
                        if dm.max_output_tokens is not None
                        else existing.max_output_tokens,
                        "capabilities": merged_caps,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                if dm.input_cost_per_1m_usd is not None:
                    updated.cost.input_cost_per_1m_usd = dm.input_cost_per_1m_usd
                if dm.output_cost_per_1m_usd is not None:
                    updated.cost.output_cost_per_1m_usd = dm.output_cost_per_1m_usd
                self._models[dm.canonical_model_id] = updated
                synced.append(updated)
            else:
                # Ensure all CapabilityFlags exist in dict, defaulting to UNKNOWN
                full_caps = build_capability_map(set(), set())
                full_caps.update(dm.capabilities)

                in_mods = [ModalityType.TEXT]
                if full_caps.get(CapabilityFlag.VISION) == CapabilityState.SUPPORTED:
                    in_mods.append(ModalityType.IMAGE)
                out_mods = [ModalityType.TEXT]
                if (
                    full_caps.get(CapabilityFlag.EMBEDDINGS)
                    == CapabilityState.SUPPORTED
                ):
                    out_mods = [ModalityType.EMBEDDING]
                elif full_caps.get(CapabilityFlag.TTS) == CapabilityState.SUPPORTED:
                    out_mods = [ModalityType.AUDIO]

                new_meta = ModelMetadata(
                    model_id=dm.canonical_model_id,
                    provider_id=dm.provider_id,
                    provider_model_id=dm.provider_model_id,
                    display_name=dm.display_name,
                    discovery_source=DiscoverySource.PROVIDER_DISCOVERY,
                    supported_protocols=dm.supported_protocols,
                    input_modalities=in_mods,
                    output_modalities=out_mods,
                    context_length=dm.context_length,  # Remains None if provider did not return it
                    max_output_tokens=dm.max_output_tokens,
                    capabilities=full_caps,
                    cost=CostMetadata(
                        input_cost_per_1m_usd=dm.input_cost_per_1m_usd,
                        output_cost_per_1m_usd=dm.output_cost_per_1m_usd,
                    ),
                    latency=LatencyStatistics(),
                    raw_metadata=dm.raw_metadata,
                )
                self._models[dm.canonical_model_id] = new_meta
                synced.append(new_meta)
        return synced

    def get_model(self, model_id: str) -> ModelMetadata:
        if model_id in self._models:
            return self._models[model_id]
        # Also allow lookup by bare provider_model_id if unambiguous
        for m in self._models.values():
            if m.provider_model_id == model_id:
                return m
        raise ResourceNotFoundError(
            f"Model {model_id!r} is not registered in the Model Registry",
            param="model_id",
        )

    def list_models(
        self,
        *,
        provider_id: str | None = None,
        enabled_only: bool = False,
    ) -> list[ModelMetadata]:
        items = list(self._models.values())
        if provider_id:
            items = [m for m in items if m.provider_id == provider_id]
        if enabled_only:
            items = [m for m in items if m.enabled]
        return sorted(items, key=lambda m: m.model_id)

    def update_model_health(
        self, model_id: str, health_status: HealthStatus
    ) -> ModelMetadata:
        model = self.get_model(model_id)
        model.health_status = health_status
        model.updated_at = datetime.now(timezone.utc)
        return model

    def is_alias(self, identifier: str) -> bool:
        return identifier.strip().lower() in self._aliases

    def get_alias(self, alias: str) -> ModelAliasSpec:
        key = alias.strip().lower()
        spec = self._aliases.get(key)
        if spec is None:
            raise ResourceNotFoundError(
                f"Model alias {alias!r} is not defined", param="alias"
            )
        return spec

    def list_aliases(self) -> list[ModelAliasSpec]:
        return sorted(self._aliases.values(), key=lambda a: a.alias)

    def upsert_alias(self, spec: ModelAliasSpec) -> ModelAliasSpec:
        key = spec.alias.strip().lower()
        normalized = spec.model_copy(update={"alias": key})
        self._aliases[key] = normalized
        return normalized

    def register_alias(self, spec: ModelAliasSpec) -> ModelAliasSpec:
        return self.upsert_alias(spec)
