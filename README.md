# Universal AI Gateway

A production-oriented, self-hosted **Universal AI Gateway** providing multi-provider connectivity, multi-key rotation with shared account-group quota realism, capability-filtered intelligent model routing, lossless native protocol passthrough, and universal tool/MCP orchestration.

---

## Key Capabilities

- **Three Explicit Integration Modes**:
  1. **Unified API**: Normalized OpenAI-compatible and universal endpoints with intelligent model routing and cross-provider fallback.
  2. **Native API Passthrough**: Zero-loss native protocol endpoints for OpenAI (Chat & Responses API), Anthropic Messages, Google Gemini, ElevenLabs Audio, and Ollama.
  3. **Custom Provider & Protocol Plugins**: Declarative REST provider integration with custom authentication, transformations, streaming parsers, and error mappings.
- **Two-Stage Intelligent Routing**:
  - **Stage 1**: Hard capability and context-window filtering (`SUPPORTED` vs `UNSUPPORTED` vs `UNKNOWN`—never fabricating missing provider metadata).
  - **Stage 2**: Configurable weighted scoring (`Quality 30%`, `Reliability 25%`, `Cost 20%`, `Latency 20%`, `Available Capacity 5%`) with full `RoutingDecisionTrace` explainability.
- **Multi-Key Credential & Quota Manager**:
  - `AES-256-GCM` envelope encryption at rest, zero-exposure Admin APIs, Redis-coordinated concurrency leases, rate-limit-aware selection, and explicit `AccountGroup` quota tracking.
- **Observability & Reliability**:
  - OpenTelemetry tracing, Prometheus metrics, secret-redacting structured JSON logs (`structlog`), circuit breakers, and safe fallback compatibility guards.

---

## Architecture & Design Documentation

- [System Architecture (`ARCHITECTURE.md`)](./ARCHITECTURE.md)
- [Technical Specification (`TECHNICAL_SPECIFICATION.md`)](./TECHNICAL_SPECIFICATION.md)
- [Module Design (`MODULE_DESIGN.md`)](./MODULE_DESIGN.md)
- [API Design (`API_DESIGN.md`)](./API_DESIGN.md)
- [Security Design (`SECURITY_DESIGN.md`)](./SECURITY_DESIGN.md)
- [Implementation Roadmap (`IMPLEMENTATION_ROADMAP.md`)](./IMPLEMENTATION_ROADMAP.md)

---

## Quick Start (Local Development)

### 1. Environment Setup

```bash
cp .env.example .env
pip install -e ".[dev]"
```

### 2. Start Infrastructure (PostgreSQL & Redis)

```bash
docker compose up -d postgres redis
alembic upgrade head
```

### 3. Run the Gateway

```bash
uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Verify Health & Readiness

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
curl http://localhost:8000/api/v1/version
```

### 5. Run Tests

```bash
pytest -v
```

