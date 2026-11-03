# Universal AI Gateway

A production-oriented, self-hosted **Universal AI Gateway** providing multi-provider connectivity, multi-key rotation with shared account-group quota realism, capability-filtered intelligent model routing, lossless native protocol passthrough, universal tool/MCP orchestration, resilient circuit-broken fallbacks, and a Next.js 15 operational control-plane dashboard.

---

## Key Capabilities

- **Three Explicit Integration Modes**:
  1. **Unified API**: Normalized OpenAI-compatible (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) and universal endpoints with semantic model aliases (`auto`, `fast`, `coding`, `reasoning`, `balanced`, `classification`, `summarization`, `speech`, `embedding`, `vision`) and cross-provider fallback.
  2. **Native API Passthrough**: Zero-loss native protocol endpoints for OpenAI (`/v1/chat/completions`, `/v1/responses`), Anthropic Messages (`/v1/messages`), Google Gemini (`/v1beta/models/{model}:generateContent` & `:streamGenerateContent`), ElevenLabs Audio (`/v1/text-to-speech/{voice_id}`, `/v1/speech-to-text`, `/v1/voices`), Ollama, and explicit `/v1/native/{provider_id}/{path}`.
  3. **Custom Provider & Protocol Plugins**: Declarative REST provider integration (`POST /api/v1/providers/custom`, `POST /v1/custom/{provider_id}`) with SSRF protection, configurable JSONPath request/response transformations, streaming parsers (`sse_json`, `ndjson`, `raw_text`), and error mappings.
- **Two-Stage Intelligent Routing**:
  - **Stage 1**: Hard capability and context-window filtering (`SUPPORTED` vs `UNSUPPORTED` vs `UNKNOWN` — never fabricating missing provider metadata).
  - **Stage 2**: Configurable weighted scoring (`Quality 30%`, `Reliability 25%`, `Cost 20%`, `Latency 20%`, `Available Capacity 5%` for `balanced`, plus `quality_first`, `cost_optimized`, `latency_optimized`, `reliability_first`) with full `RoutingDecisionTrace` explainability (`GET /api/v1/routing/explain/{decision_id}`).
- **Multi-Key Credential & Quota Manager**:
  - Two-tier `AES-256-GCM` envelope encryption at rest, zero-exposure RBAC Admin APIs, Redis-coordinated concurrency leases, 5 selection strategies (`ROUND_ROBIN`, `WEIGHTED`, `LEAST_ACTIVE`, `LEAST_RECENTLY_USED`, `RATE_LIMIT_AWARE`), and explicit `AccountGroup` shared organization/workspace quota tracking.
- **Universal Tool Gateway & MCP Integration**:
  - Strict JSON Schema validation, built-in tools (`gateway_current_utc_time`, `gateway_calculator`, `gateway_json_validator`), SSRF-guarded webhook tools, JSON-RPC 2.0 MCP client (`initialize`, `tools/list`, `tools/call`), and multi-turn `gateway_managed` tool execution loops.
- **Resilience, Fallback & Observability**:
  - Redis-coordinated tri-state circuit breakers (`CLOSED`, `OPEN`, `HALF_OPEN`), `FallbackCompatibilityGuard` enforcing pre-first-byte streaming safety and tool idempotency, verified USD cost tracking, Prometheus `/metrics`, W3C `traceparent` spans, and Next.js 15 Management Dashboard (`dashboard/`).

---

## Architecture, Audit & Engineering Documentation

### Architecture & Design
- [System Architecture (`ARCHITECTURE.md`)](./ARCHITECTURE.md)
- [Technical Specification (`TECHNICAL_SPECIFICATION.md`)](./TECHNICAL_SPECIFICATION.md)
- [Module Design (`MODULE_DESIGN.md`)](./MODULE_DESIGN.md)
- [API Design (`API_DESIGN.md`)](./API_DESIGN.md)
- [Security Design (`SECURITY_DESIGN.md`)](./SECURITY_DESIGN.md)
- [Implementation Roadmap (`IMPLEMENTATION_ROADMAP.md`)](./IMPLEMENTATION_ROADMAP.md)

### Verification & Audit Reports
- [Full System Test Report (`TEST_REPORT.md`)](./TEST_REPORT.md)
- [Security Audit Report (`SECURITY_AUDIT.md`)](./SECURITY_AUDIT.md)
- [Performance & Concurrency Report (`PERFORMANCE_REPORT.md`)](./PERFORMANCE_REPORT.md)
- [Known Limitations & Tradeoffs (`KNOWN_LIMITATIONS.md`)](./KNOWN_LIMITATIONS.md)
- [Changelog (`CHANGELOG.md`)](./CHANGELOG.md)
- [Contributing Guide (`CONTRIBUTING.md`)](./CONTRIBUTING.md)

---

## Quick Start

### Option A: Local Zero-External-Dependency Mode (SQLite + In-Memory Redis)

```bash
cp .env.example .env
pip install -e ".[dev]"
uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000
```

### Option B: Full Stack with Docker Compose (PostgreSQL + Redis + Gateway + Next.js Dashboard)

```bash
cp .env.example .env
docker compose up --build -d
```

- **Gateway API & OpenAPI Docs**: `http://localhost:8000/api/v1/docs`
- **Prometheus Metrics**: `http://localhost:8000/metrics`
- **Next.js Management Dashboard**: `http://localhost:3000`

---

## Example API Usage

### 1. Unified Chat Completion with Semantic Alias & Automatic Routing

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-provider-key" \
  -H "X-Gateway-Mode: unified" \
  -H "X-Gateway-Policy: balanced" \
  -d '{
    "model": "coding",
    "messages": [
      {"role": "user", "content": "Write an async token bucket in Python."}
    ]
  }'
```

### 2. Inspect Explainable Routing Decision Trace

```bash
# Use the X-Gateway-Route-Decision-Id response header from the call above:
curl http://localhost:8000/api/v1/routing/explain/<decision_id>
```

### 3. Simulate a Routing Decision Without Upstream Execution

```bash
curl -X POST http://localhost:8000/api/v1/routing/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "requested_model": "reasoning",
    "policy_name": "quality_first",
    "required_capabilities": ["chat_completions", "tool_calling"]
  }'
```

### 4. Register an Encrypted Provider Credential (Admin API)

```bash
curl -X POST http://localhost:8000/api/v1/admin/credentials \
  -H "Content-Type: application/json" \
  -H "X-Gateway-Admin-Token: change-me-in-production-admin-token" \
  -H "X-Gateway-Admin-Role: SUPER_ADMIN" \
  -d '{
    "provider_id": "openai",
    "label": "OpenAI Production Key #1",
    "secret": "sk-proj-your-real-openai-key",
    "weight": 100,
    "max_concurrency": 50
  }'
```

---

## Running the Automated Test Suite

```bash
pytest -v
```

---

## License

Released under the [MIT License](./LICENSE).
