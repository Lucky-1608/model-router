# Contributing to Universal AI Gateway (`CONTRIBUTING.md`)

Thank you for contributing to the **Universal AI Gateway**! Please follow these engineering, architectural, and security standards when developing features, fixing bugs, or adding provider/protocol adapters.

---

## 1. Core Engineering Rules

1. **Never Hardcode or Commit Secrets**:
   - All tests must use synthetic test credentials (`sk-test-...`) or mock HTTP providers (`respx`).
   - `.env` files are strictly gitignored.
   - Any new sensitive header or payload field must be registered in [`SENSITIVE_HEADER_KEYS`](file:///c:/Users/vuppa/Documents/projects/model-router/src/gateway/core/security.py) so `structlog` and trace sanitizers redact it automatically.
2. **Never Fabricate Provider or Model Capabilities**:
   - When adding or updating a provider or model manifest, only mark a capability as `SUPPORTED` when verified against official provider documentation and covered by integration tests. Otherwise, use `UNKNOWN` or `UNSUPPORTED`.
3. **Never Silently Drop Unsupported Provider Parameters**:
   - In Unified Mode, if a request requires a capability or parameter not supported by the target provider, raise `UnsupportedCapabilityError` (`422 GW_UNSUPPORTED_CAPABILITY`).
   - In Native Mode, preserve provider-native request/response bodies and SSE event framings without lossy normalization.
4. **Preserve Module Boundaries**:
   - Follow the 17-module Directed Acyclic Graph (DAG) defined in [`MODULE_DESIGN.md`](./MODULE_DESIGN.md).
   - Routing and orchestration layers must never contain hardcoded provider-specific conditionals; use `BaseProviderAdapter` and `ProviderCapabilityManifest`.
5. **Explicit Timeouts & Resource Cleanup**:
   - Every outbound HTTP request, database query, Redis command, and tool invocation must define explicit timeouts via `HTTPXClientPoolManager` (`TimeoutPolicy`), `DatabaseManager`, or `asyncio.wait_for`.

---

## 2. How to Add a New Provider Adapter

1. Create `src/gateway/provider_adapters/<provider>_adapter.py` subclassing [`BaseProviderAdapter`](file:///c:/Users/vuppa/Documents/projects/model-router/src/gateway/provider_adapters/base.py).
2. Implement `get_capability_manifest()` with verified `CapabilityFlag` states and `QuotaScopeType` scopes.
3. Implement `discover_models()`, `execute()`, `execute_stream()`, `extract_rate_limits()`, `classify_error()`, and `check_health()`.
4. Register the adapter in [`ProviderRegistry.register_builtin_adapters()`](file:///c:/Users/vuppa/Documents/projects/model-router/src/gateway/provider_registry/registry.py).
5. Add mocked `respx` integration tests in [`tests/test_provider_adapters.py`](file:///c:/Users/vuppa/Documents/projects/model-router/tests/test_provider_adapters.py).

---

## 3. Running Tests & Quality Checks

```bash
# Run full automated test suite (61 tests)
pytest -v

# Run dashboard TypeScript typecheck
cd dashboard && npm run typecheck
```
