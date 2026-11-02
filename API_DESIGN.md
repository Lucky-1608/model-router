# Universal AI Gateway — API Design (`API_DESIGN.md`)

## 1. API Surface Overview & Versioning

The Universal AI Gateway exposes four clearly separated route namespaces:

| Namespace | Prefix | Purpose | Auth Mechanism |
| :--- | :--- | :--- | :--- |
| **Unified & OpenAI-Compatible API** | `/v1/...` | Drop-in OpenAI SDK compatibility + universal routing, aliases (`model="coding"`), and cross-provider fallback. | `Authorization: Bearer ugw_live_...` or `X-Gateway-Api-Key` |
| **Native Provider APIs** | `/v1/native/{provider}/...` or provider-standard paths (`/v1/messages`, `/v1beta/models/...`) | Zero-loss native protocol support for Anthropic, Gemini, OpenAI Responses, ElevenLabs, Ollama, etc. | Native header (`x-api-key`, `x-goog-api-key`, `xi-api-key`, `Authorization`) carrying a Gateway Client Key |
| **Custom Plugin APIs** | `/v1/custom/{plugin_slug}/...` | Invokes user-registered custom REST AI providers. | Gateway Client Key |
| **Admin & Analytics API** | `/api/v1/admin/...` | Configuration of providers, credentials, quotas, routing policies, MCP servers, and analytics. | Admin JWT / RBAC Session or Admin API Key (`ugw_admin_...`) |
| **System & Observability** | `/health/live`, `/health/ready`, `/metrics` | Kubernetes/Docker probes and Prometheus scrape target. | Public (probes) / Internal token (`/metrics`) |

---

## 2. Gateway Custom Control Headers

Clients using **any** SDK (OpenAI, Anthropic, Gemini, HTTPX) can pass optional Gateway control headers without breaking upstream schema validation (the gateway strips `X-Gateway-*` headers before forwarding upstream):

| Request Header | Values / Format | Description |
| :--- | :--- | :--- |
| `X-Gateway-Policy` | `balanced` \| `quality_first` \| `cost_optimized` \| `latency_optimized` \| `reliability_first` | Overrides the default routing policy for this request. |
| `X-Gateway-Task` | `coding` \| `reasoning` \| `fast` \| `summarization` \| `classification` \| `vision` | Provides an explicit task hint to the Routing Engine when not using a model alias. |
| `X-Gateway-Fallback` | `true` \| `false` | Enables or disables cross-provider/model fallback for this request. |
| `X-Gateway-Allowed-Providers` | `openai,anthropic,gemini` | Comma-separated allowlist of providers for this request. |
| `X-Gateway-Max-Cost-USD` | Float (e.g., `0.05`) | Hard ceiling on estimated request cost. |
| `X-Gateway-Explain-Route` | `true` \| `false` | If `true`, includes a summary routing trace header or response extension. |
| `X-Gateway-Tool-Mode` | `passthrough` \| `gateway_managed` | Controls whether MCP/Gateway tools are executed automatically by the gateway. |

### Response Headers Returned by Gateway
- `X-Gateway-Request-Id`: UUID of the gateway request.
- `X-Gateway-Provider`: Selected upstream provider slug (e.g., `anthropic`).
- `X-Gateway-Model`: Selected upstream model ID (e.g., `claude-3-5-sonnet-20241022`).
- `X-Gateway-Credential-Id`: Non-sensitive UUID of the credential used (never the key value).
- `X-Gateway-Attempts`: Number of upstream attempts (`1` if primary succeeded).
- `X-Gateway-Latency-Overhead-Ms`: Gateway internal routing + orchestration time in milliseconds.

---

## 3. Unified & Protocol-Compatible Endpoints

### 3.1 OpenAI-Compatible Interface
- `POST /v1/chat/completions`
  - Supports standard messages, streaming (`stream: true` with `stream_options.include_usage`), multi-modal content parts (`text`, `image_url`, `input_audio`), `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` (`text`, `json_object`, `json_schema`), and `reasoning_effort`.
  - Accepts either an explicit `provider/model` (`openai/gpt-4o`, `anthropic/claude-3-5-sonnet-latest`), a bare model name (`gpt-4o`), or a **Model Alias** (`auto`, `fast`, `coding`, `reasoning`, `balanced`, `classification`, `summarization`, `vision`).
- `POST /v1/embeddings`
  - Supports text and batch array inputs, `dimensions`, `encoding_format`, and alias `model="embedding"`.
- `GET /v1/models`
  - Lists all enabled models across active providers plus configured model aliases, augmented with capability metadata under `x_gateway_capabilities`.

### 3.2 OpenAI Responses API Interface
- `POST /v1/responses`
- `GET /v1/responses/{response_id}`
  - Preserves native OpenAI Responses semantics: polymorphic `input` (string or array of input items: `message`, `function_call_output`, `item_reference`), `instructions`, `tools` (both function tools and provider-hosted tools like `web_search_preview`), `text.format` structured output schemas, and native SSE event types (`response.created`, `response.output_item.added`, `response.output_text.delta`, `response.completed`).

### 3.3 Anthropic Native Interface
- `POST /v1/messages` and `POST /v1/native/anthropic/v1/messages`
- `POST /v1/messages/count_tokens`
  - Preserves Anthropic's native schema: top-level `system` (string or array of content blocks with `cache_control`), `messages` (`text`, `image`, `document`, `tool_use`, `tool_result`, `thinking`, `redacted_thinking`), `tools` (`input_schema`), `thinking` (`{"type": "enabled", "budget_tokens": ...}`), and `anthropic-beta` headers.
  - Emits exact Anthropic SSE events (`event: message_start`, `event: content_block_start`, `event: ping`, `event: content_block_delta`, `event: message_delta`, `event: message_stop`).

### 3.4 Google Gemini Native Interface
- `POST /v1beta/models/{model}:generateContent`
- `POST /v1beta/models/{model}:streamGenerateContent`
- `POST /v1beta/models/{model}:embedContent`
- `GET /v1beta/models`
  - Preserves `contents` (`role: "user" | "model"`, `parts`), `systemInstruction`, `tools` (`functionDeclarations`), `toolConfig` (`functionCallingConfig`), `safetySettings`, and `generationConfig` (`responseMimeType`, `responseSchema`, `thinkingConfig`).

### 3.5 ElevenLabs Native Interface
- `POST /v1/text-to-speech/{voice_id}`
- `POST /v1/text-to-speech/{voice_id}/stream`
- `POST /v1/speech-to-text`
- `GET /v1/voices`
  - Supports query parameters (`output_format=mp3_44100_128`, `optimize_streaming_latency`), `voice_settings` (`stability`, `similarity_boost`, `style`, `use_speaker_boost`), binary audio chunk streaming, and character-based quota accounting.

---

## 4. Routing & Explainability API

- `POST /api/v1/routing/simulate` (or `POST /api/v1/routing/route`)
  - Executes the full Stage 1 (Capability Filter) and Stage 2 (Weighted Scoring) pipeline without invoking an upstream provider, returning the complete `RoutingDecisionTrace`.
- `GET /api/v1/routing/explain/{request_id}`
  - Retrieves the persisted `RoutingDecisionTrace` for any historical gateway request:
    ```json
    {
      "request_id": "0192eb10-84a0-7000-8000-000000000001",
      "requested_model_or_alias": "coding",
      "requested_task": "coding",
      "applied_policy": "balanced",
      "weights": {"quality": 0.30, "reliability": 0.25, "cost": 0.20, "latency": 0.20, "capacity": 0.05},
      "required_capabilities": ["tool_calling", "streaming"],
      "estimated_input_tokens": 4200,
      "candidates_evaluated": 9,
      "filtered_out": [
        {
          "canonical_model_id": "ollama/llama3.2-1b",
          "reason_code": "UNSUPPORTED_CAPABILITY",
          "details": "Capability 'tool_calling' is UNKNOWN"
        }
      ],
      "ranked_candidates": [
        {
          "canonical_model_id": "anthropic/claude-3-5-sonnet-latest",
          "total_score": 0.892,
          "dimension_scores": {"quality": 0.98, "reliability": 0.99, "cost": 0.62, "latency": 0.81, "capacity": 0.90}
        },
        {
          "canonical_model_id": "openai/gpt-4o",
          "total_score": 0.864,
          "dimension_scores": {"quality": 0.94, "reliability": 0.98, "cost": 0.68, "latency": 0.78, "capacity": 0.95}
        }
      ],
      "selected_model": "anthropic/claude-3-5-sonnet-latest",
      "selected_provider": "anthropic",
      "selected_credential_ref": "cred_0192eb01 (Anthropic Prod Key #2, masked: sk-ant-...4e1a)",
      "fallback_attempts": []
    }
    ```

---

## 5. Admin & Management API (`/api/v1/admin/...`)

1. **Providers**: `GET / POST / PATCH / DELETE /api/v1/admin/providers`, `POST /api/v1/admin/providers/{id}/discover-models`, `POST /api/v1/admin/providers/{id}/health-check`
2. **Credentials & Account Groups**:
   - `GET / POST / PATCH / DELETE /api/v1/admin/account-groups`
   - `GET / POST / PATCH / DELETE /api/v1/admin/credentials`
   - `POST /api/v1/admin/credentials/{id}/validate`
   - `POST /api/v1/admin/credentials/{id}/enable` | `/disable` | `/revoke`
   - `GET /api/v1/admin/credentials/{id}/health` (returns live Redis quota, cooldown, and concurrency metrics; **never** returns secret key material)
3. **Models & Aliases**: `GET / POST / PATCH / DELETE /api/v1/admin/models`, `GET / PUT /api/v1/admin/aliases`
4. **Routing Policies**: `GET / POST / PATCH / DELETE /api/v1/admin/routing-policies`
5. **Tools & MCP Servers**:
   - `GET / POST / PATCH / DELETE /api/v1/admin/tools`
   - `GET / POST / PATCH / DELETE /api/v1/admin/mcp-servers`
   - `POST /api/v1/admin/mcp-servers/{id}/sync-tools`
6. **Client Gateway API Keys**: `GET / POST / DELETE /api/v1/admin/client-keys`
7. **Analytics & Observability**:
   - `GET /api/v1/admin/analytics/overview`
   - `GET /api/v1/admin/analytics/models`
   - `GET /api/v1/admin/analytics/credentials`
   - `GET /api/v1/admin/analytics/routing`
   - `GET /api/v1/admin/logs/requests`
   - `GET /api/v1/admin/logs/audit`

---

## 6. Standardized Error Taxonomy

In **Unified Mode**, errors return a consistent envelope compatible with OpenAI/standard clients while adding machine-readable gateway metadata. In **Native Mode**, upstream provider errors are passed through in the provider's native error schema unless the failure originated inside the gateway (e.g., no healthy credentials available).

```json
{
  "error": {
    "message": "No candidate models satisfy required capability: structured_outputs",
    "type": "unsupported_capability_error",
    "code": "GW_UNSUPPORTED_CAPABILITY",
    "param": "response_format",
    "gateway_metadata": {
      "request_id": "0192eb10-84a0-7000-8000-000000000001",
      "retryable": false,
      "routing_trace_id": "0192eb10-84a0-7000-8000-000000000002"
    }
  }
}
```

