# Changelog (`CHANGELOG.md`)

All notable changes to the **Universal AI Gateway** project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-11-03

### Added
- **Phase 1 — Architecture & Repository Foundation**:
  - Complete architectural specifications (`ARCHITECTURE.md`, `TECHNICAL_SPECIFICATION.md`, `MODULE_DESIGN.md`, `API_DESIGN.md`, `SECURITY_DESIGN.md`, `IMPLEMENTATION_ROADMAP.md`).
  - FastAPI application lifecycle (`src/gateway/main.py`), Pydantic v2 settings validation (`src/gateway/config/settings.py`), SQLAlchemy 2.0 async engine + Alembic migrations (`src/gateway/core/db.py`), Redis async pool with `InMemoryRedisFallback` (`src/gateway/core/redis.py`), four-phase timeout HTTPX connection pool (`src/gateway/core/http.py`), and `structlog` secret-redacting logger (`src/gateway/core/logging.py`).
- **Phase 2 — Provider & Protocol Infrastructure**:
  - `ProviderRegistry` and `BaseProviderAdapter` contract with explicit capability states (`SUPPORTED`, `UNSUPPORTED`, `UNKNOWN`).
  - 7 built-in verified provider adapters (**OpenAI**, **Anthropic**, **Google Gemini**, **OpenRouter**, **Groq**, **Ollama**, **ElevenLabs**) + configurable `CustomHttpProviderAdapter` with SSRF protection.
  - Universal Protocol Compatibility Layer supporting OpenAI Chat Completions (`/v1/chat/completions`), Embeddings (`/v1/embeddings`), Responses API (`/v1/responses`), Anthropic Messages (`/v1/messages`), Gemini Native (`/v1beta/models/...`), ElevenLabs Audio (`/v1/text-to-speech`, `/v1/speech-to-text`, `/v1/voices`), Custom REST protocols (`/v1/custom/{id}`), and zero-normalization Native Passthrough (`/v1/native/{id}/{path}`).
- **Phase 3 — Multi-Key Credential & Quota Manager**:
  - Two-tier `AES-256-GCM` envelope encryption (`EnvelopeEncryptor`) with unique 256-bit DEK per credential and AAD binding.
  - Full credential lifecycle (`ACTIVE`, `DISABLED`, `INVALID`, `COOLING_DOWN`, `TEMPORARILY_UNAVAILABLE`, `REQUIRES_ADMIN_ATTENTION`, `REVOKED`), 5 selection algorithms (`ROUND_ROBIN`, `WEIGHTED`, `LEAST_ACTIVE`, `LEAST_RECENTLY_USED`, `RATE_LIMIT_AWARE`), shared `AccountGroup` organization quota tracking (`QuotaManager`), and immutable audit logging.
- **Phase 4 — Model Registry & Two-Stage Intelligent Routing Engine**:
  - `ModelRegistry` with 10 semantic model aliases (`auto`, `fast`, `coding`, `reasoning`, `balanced`, `classification`, `summarization`, `speech`, `embedding`, `vision`).
  - `RoutingPolicyEngine` and `RoutingEngine` implementing Stage 1 hard capability/context/cost filtering, Stage 2 deterministic weighted scoring (`Quality 30%`, `Reliability 25%`, `Cost 20%`, `Latency 20%`, `Capacity 5%`), and full `RoutingDecisionTrace` explainability (`/api/v1/routing/explain/{decision_id}`).
- **Phase 5 — Universal Tool Gateway & MCP Integration**:
  - `ToolRegistry` and `ToolGatewayOrchestrator` with strict JSON Schema validation, built-in tools, SSRF-guarded webhook tools, and provider-native tool non-portability enforcement.
  - `MCPClientManager` supporting JSON-RPC 2.0 `initialize`, `tools/list`, and `tools/call` with `allowed_tools`/`blocked_tools` filtering and multi-turn `gateway_managed` execution loops.
- **Phase 6 — Reliability, Fallback Engine, Usage Analytics, & Observability**:
  - `HealthMonitor` with Redis-coordinated tri-state circuit breakers (`CLOSED`, `OPEN`, `HALF_OPEN`) across provider, model, and credential scopes plus EWMA latency/reliability updates.
  - `FallbackCompatibilityGuard` and `RetryFallbackEngine` enforcing pre-first-byte streaming safety (`bytes_sent_to_client == 0`), tool idempotency, and mandatory capability parity.
  - `UsageTracker` for verified USD cost calculation, `MetricsCollector` exposing Prometheus text format (`/metrics`), `TracingManager` with W3C `traceparent` propagation, and `/api/v1/analytics/*` endpoints.
- **Phase 7 — Admin Control Plane API & Next.js Management Dashboard**:
  - `AdminControlPlaneManager` for SHA-256-hashed Client Gateway API Keys (`ugw_live_...`), sanitized system settings, and unified dashboard snapshots.
  - Next.js 15 + TypeScript + Tailwind CSS Management Dashboard (`dashboard/`) covering all 12 operational views with RBAC role switching and explicit `LIVE_BACKEND` vs `DEMO_FALLBACK` mode separation.
- **Phase 8 & 9 — Full System Testing, Security Audit, & Open-Source Release**:
  - 61 automated tests (`pytest -v`) including 30-worker concurrent load, cascading failure recovery, and SSRF/RBAC/canary-secret audits (`TEST_REPORT.md`, `SECURITY_AUDIT.md`, `PERFORMANCE_REPORT.md`, `KNOWN_LIMITATIONS.md`).

