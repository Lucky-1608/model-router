# Universal AI Gateway — Known Limitations & Operational Tradeoffs (`KNOWN_LIMITATIONS.md`)

In accordance with our engineering principles (never fabricating capabilities or hiding architectural tradeoffs), this document records the current boundaries and design tradeoffs of the Universal AI Gateway.

---

## 1. Protocol & Capability Boundaries

1. **Provider-Native Tools Are Non-Portable**:
   - Provider-hosted server-side tools (such as OpenAI `web_search_preview`, `file_search`, `computer_use_preview`, Anthropic `computer_20250124`, `bash_20250124`, and Google `googleSearch` grounding) execute inside the upstream provider's proprietary infrastructure.
   - The gateway **intentionally refuses** (`422 GW_UNSUPPORTED_CAPABILITY` or `FallbackCompatibilityGuard` rejection) to translate or fallback these tools to another provider. For cross-provider portable tools, register tools in the Gateway `ToolRegistry` or connect an MCP server.
2. **Pre-First-Byte Streaming Fallback Only**:
   - For streaming requests (`stream: true`), `FallbackCompatibilityGuard` permits switching to a fallback model/provider **only before the first chunk is yielded to the client** (`bytes_sent_to_client == 0`).
   - If an upstream provider fails mid-stream after partial tokens have already been delivered to the client, the gateway records the failure in `HealthMonitor` and terminates the stream cleanly rather than corrupting the client's stream with a second model's output.
3. **Unverified Dynamic Models Default to `UNKNOWN` Capabilities**:
   - When discovering newly released models from providers (e.g., via `/v1/models` on Ollama or OpenAI) that are not in the curated capability manifest, capabilities such as `tool_calling` or `structured_outputs` are marked `UNKNOWN` rather than `SUPPORTED`. Operators can promote them to `SUPPORTED` via `POST /api/v1/routing/models` once verified.

---

## 2. Quota & Rate-Limit Accounting

1. **Shared Organization Quotas Require Explicit `AccountGroup` Assignment**:
   - Most cloud AI providers (OpenAI, Anthropic, Gemini) enforce rate limits (RPM/TPM) at the **organization or workspace level** rather than per API key.
   - Operators adding multiple keys from the same organization should assign them to the same `account_group_id` so `QuotaManager` coordinates rate-limit cooldowns across all keys in that account.
2. **Pre-Request Token Estimation vs Post-Request Reconciliation**:
   - Before an upstream call completes, input tokens are estimated heuristically (`chars / 4`) for Stage 1 context-window filtering. Exact token counts and USD costs are reconciled after receiving the upstream response `usage` block.

---

## 3. State Persistence & Deployment Topology

1. **In-Memory Routing Traces & Span Buffers**:
   - While credentials, account groups, client API keys, audit logs, and usage records are persisted in SQL (`PostgreSQL` / `SQLite`), detailed `RoutingDecisionTrace` explanations and `TracingManager` span buffers are kept in bounded in-memory ring buffers (`deque(maxlen=1000)`) for low-latency inspection. For long-term distributed trace retention, point OpenTelemetry exporters to Jaeger/Tempo/OTLP collectors.
2. **Stdio MCP Servers**:
   - The gateway's `MCPClientManager` supports `http`, `sse`, and `memory` JSON-RPC 2.0 transports out of the box with SSRF protection. Local subprocess (`stdio`) MCP servers are disabled by default to preserve container sandbox isolation.

