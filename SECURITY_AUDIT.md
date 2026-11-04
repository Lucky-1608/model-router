# Universal AI Gateway — Security Audit Report (`SECURITY_AUDIT.md`)

## 1. Security Architecture & Threat Model Verification

This audit verifies the implementation of the security controls specified in [`SECURITY_DESIGN.md`](./SECURITY_DESIGN.md) across all 17 backend modules and the Next.js management dashboard.

---

## 2. Cryptographic & Credential Controls

| Control Area | Implementation | Verification Evidence | Status |
| :--- | :--- | :--- | :---: |
| **Envelope Encryption at Rest** | Every provider API key is encrypted with a unique random 256-bit Data Encryption Key (DEK) and 96-bit nonce using `AES-256-GCM`, and the DEK is wrapped by the 256-bit Master Key Encryption Key (`MASTER_KEK_BASE64`) with AAD binding (`provider_id:credential_id`). | [`crypto.py`](./src/gateway/credential_manager/crypto.py) & `test_envelope_encryption_roundtrip_and_tamper_detection` | **VERIFIED** |
| **Ciphertext Tamper Detection** | GCM authentication tag verification rejects any modified ciphertext, nonce, wrapped DEK, or mismatched AAD (`provider_id`/`credential_id` swap). | `test_envelope_encryption_roundtrip_and_tamper_detection` | **VERIFIED** |
| **Client API Key Hashing** | Gateway client keys (`ugw_live_...`) are hashed with `SHA-256` before persistence (`key_hash`). The plaintext key is returned only once in the `201 Created` response. | [`admin_api/manager.py`](./src/gateway/admin_api/manager.py) & `test_admin_client_api_key_lifecycle_rbac_and_hash_only_storage` | **VERIFIED** |
| **In-Memory Secret Isolation** | `AdapterCredential` uses `pydantic.SecretStr` and overrides `__repr__` and `__str__` to output `secret='[REDACTED]'`. | [`canonical.py`](./src/gateway/core/canonical.py) | **VERIFIED** |
| **Canary Secret Non-Leakage** | A canary secret (`sk-proj-CANARY_ULTRA_SECRET_...`) was injected and verified absent across all 9 admin, health, analytics, audit, and `/metrics` endpoints. | `test_security_boundary_audit_ssrf_rbac_and_zero_secret_leakage` | **VERIFIED** |

---

## 3. Network & SSRF Boundary Controls

| Attack Vector | Mitigation | Verification Evidence | Status |
| :--- | :--- | :--- | :---: |
| **Cloud Metadata SSRF (`169.254.169.254`)** | Blocked by [`validate_outbound_url`](./src/gateway/tool_gateway/registry.py) and [`CustomHttpProviderAdapter.validate_base_url`](./src/gateway/provider_adapters/custom_adapter.py) across custom providers, webhook tools, and MCP servers. | `test_security_boundary_audit_ssrf_rbac_and_zero_secret_leakage` | **VERIFIED** |
| **Loopback & RFC1918 Private Network Probing (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)** | Blocked by default (`allow_private_network=False`) for all user-supplied URLs. | `test_webhook_tool_ssrf_guard_permissions_redaction_and_timeout` & `test_custom_http_provider_adapter_and_ssrf_guard` | **VERIFIED** |
| **Non-HTTP Scheme Injection (`file://`, `gopher://`, `ftp://`)** | Only `http://` and `https://` schemes are permitted. | `validate_outbound_url` | **VERIFIED** |

---

## 4. Access Control (RBAC) & Telemetry Privacy

1. **Constant-Time Token Comparison**:
   - Administrative authentication (`require_admin_access` in [`credential_admin_router`](./src/gateway/credential_manager/router.py)) uses `hmac.compare_digest` to prevent timing side-channel attacks.
2. **Role-Based Access Control (`SUPER_ADMIN`, `OPERATOR`, `READ_ONLY`)**:
   - `READ_ONLY` callers are strictly blocked (`403 Forbidden`) from creating, updating, rotating, or deleting credentials, account groups, client API keys, or resetting circuit breakers.
3. **Zero Prompt / Completion Persistence by Default**:
   - [`UsageRecordORM`](./src/gateway/usage_tracking/models.py) and [`TraceSpan`](./src/gateway/observability/tracing.py) never store raw prompt messages, completion text, or secret headers.

