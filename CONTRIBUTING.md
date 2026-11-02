# Contributing to Universal AI Gateway

Thank you for contributing to the **Universal AI Gateway**! Please follow these engineering and security standards when developing features, fixing bugs, or adding provider/protocol adapters.

---

## Core Engineering Rules

1. **Never Hardcode or Commit Secrets**:
   - All tests must use synthetic test credentials (`sk-test-...`) or mock HTTP providers (`respx`).
   - `.env` files are strictly gitignored.
2. **Never Fabricate Provider Capabilities**:
   - When adding or updating a provider or model manifest, only mark a capability as `SUPPORTED` when verified against official provider documentation and covered by integration tests. Otherwise, use `UNKNOWN` or `UNSUPPORTED`.
3. **Preserve Module Boundaries**:
   - Follow the Directed Acyclic Graph (DAG) defined in `MODULE_DESIGN.md`.
   - Routing and orchestration layers must never contain hardcoded provider-specific conditionals; use `BaseProviderAdapter` and `ProviderCapabilityManifest`.
4. **Explicit Timeouts & Resource Cleanup**:
   - Every outbound HTTP request, database query, Redis command, and tool invocation must define explicit timeouts.
5. **Testing Requirements**:
   - Every feature or bugfix must include unit and integration tests under `tests/`.
   - Run `pytest -v` before submitting changes and verify zero regressions.

