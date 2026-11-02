"""Comprehensive unit and integration tests for Phase 3 Multi-Key Credential & Quota Manager."""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
import httpx
from pydantic import SecretStr
import pytest
import respx

from gateway.capability_registry import QuotaScopeType
from gateway.core.exceptions import RateLimitExceededError, UpstreamProviderError
from gateway.credential_manager import (
    AccountGroupCreateRequest,
    CredentialCreateRequest,
    CredentialManager,
    CredentialStatus,
    CredentialUpdateRequest,
    EnvelopeEncryptor,
    QuotaConfigSpec,
    SelectionStrategy,
)
from gateway.main import FastAPI

ADMIN_HEADERS = {"X-Gateway-Admin-Token": "ugw_admin_test_token_1234567890"}


# =============================================================================
# 1. Envelope Encryption & Cryptographic Integrity Tests
# =============================================================================
def test_envelope_encryption_roundtrip_and_tamper_detection() -> None:
    kek = b"0123456789abcdef0123456789abcdef"
    encryptor = EnvelopeEncryptor(kek)
    secret = "sk-proj-super_secret_openai_key_987654321"

    env1 = encryptor.encrypt(secret, provider_id="openai", credential_id="cred-1")
    env2 = encryptor.encrypt(secret, provider_id="openai", credential_id="cred-1")

    # Fresh DEK and nonces mean ciphertexts are distinct even for identical plaintext
    assert env1.encrypted_secret != env2.encrypted_secret
    assert env1.encrypted_dek != env2.encrypted_dek
    assert env1.key_fingerprint == env2.key_fingerprint
    assert "987654321" not in env1.masked_preview
    assert secret.encode("utf-8") not in env1.encrypted_secret

    # Decrypt succeeds with matching provider_id and credential_id AAD
    decrypted = encryptor.decrypt(
        env1.encrypted_secret,
        env1.encrypted_dek,
        provider_id="openai",
        credential_id="cred-1",
    )
    assert decrypted == secret

    # Tampered AAD (wrong credential_id or provider_id) fails authentication tag check
    with pytest.raises(InvalidTag):
        encryptor.decrypt(
            env1.encrypted_secret,
            env1.encrypted_dek,
            provider_id="openai",
            credential_id="cred-tampered",
        )


# =============================================================================
# 2. Admin API Security, RBAC, Lifecycle & Audit Logging Tests
# =============================================================================
@respx.mock
async def test_credential_admin_api_lifecycle_rbac_and_secret_redaction(
    async_client: httpx.AsyncClient,
) -> None:
    # 1. Unauthenticated request rejected with 401
    unauth = await async_client.get("/api/v1/admin/credentials")
    assert unauth.status_code == 401

    # 2. READ_ONLY role rejected on write with 403
    ro_resp = await async_client.post(
        "/api/v1/admin/credentials",
        headers={**ADMIN_HEADERS, "X-Gateway-Admin-Role": "READ_ONLY"},
        json={
            "provider_id": "openai",
            "label": "Should Fail",
            "secret": "sk-proj-1234567890abcdef",
        },
    )
    assert ro_resp.status_code == 403

    # 3. Create Credential — verify secret is NEVER returned
    raw_secret = "sk-proj-live_secret_value_do_not_expose_9999"
    create_resp = await async_client.post(
        "/api/v1/admin/credentials",
        headers=ADMIN_HEADERS,
        json={
            "provider_id": "openai",
            "label": "OpenAI Primary Key",
            "secret": raw_secret,
            "weight": 150,
            "max_concurrency": 10,
        },
    )
    assert create_resp.status_code == 201
    cred_view = create_resp.json()
    cred_id = cred_view["id"]
    assert raw_secret not in create_resp.text
    assert "secret" not in cred_view
    assert "encrypted_secret" not in cred_view
    assert cred_view["status"] == "ACTIVE"
    assert cred_view["masked_preview"].endswith("9999")

    # 4. Disable & Enable Credential
    dis_resp = await async_client.post(
        f"/api/v1/admin/credentials/{cred_id}/disable", headers=ADMIN_HEADERS
    )
    assert dis_resp.status_code == 200
    assert dis_resp.json()["status"] == "DISABLED"

    en_resp = await async_client.post(
        f"/api/v1/admin/credentials/{cred_id}/enable", headers=ADMIN_HEADERS
    )
    assert en_resp.status_code == 200
    assert en_resp.json()["status"] == "ACTIVE"

    # 5. Update Credential
    upd_resp = await async_client.patch(
        f"/api/v1/admin/credentials/{cred_id}",
        headers=ADMIN_HEADERS,
        json={"label": "OpenAI Primary Key Renamed", "weight": 200},
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["label"] == "OpenAI Primary Key Renamed"
    assert upd_resp.json()["weight"] == 200

    # 6. Validate Credential against mocked upstream (401 -> transitions to INVALID)
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "invalid_api_key", "message": "Invalid key"}},
        )
    )
    val_resp = await async_client.post(
        f"/api/v1/admin/credentials/{cred_id}/validate", headers=ADMIN_HEADERS
    )
    assert val_resp.status_code == 200
    assert val_resp.json()["status"] == "UNHEALTHY"

    health_resp = await async_client.get(
        f"/api/v1/admin/credentials/{cred_id}/health", headers=ADMIN_HEADERS
    )
    assert health_resp.status_code == 200
    assert health_resp.json()["persistent_status"] == "INVALID"
    assert health_resp.json()["is_eligible"] is False

    # 7. Revoke & Delete Credential
    rev_resp = await async_client.post(
        f"/api/v1/admin/credentials/{cred_id}/revoke", headers=ADMIN_HEADERS
    )
    assert rev_resp.status_code == 200
    assert rev_resp.json()["status"] == "REVOKED"

    del_resp = await async_client.delete(
        f"/api/v1/admin/credentials/{cred_id}", headers=ADMIN_HEADERS
    )
    assert del_resp.status_code == 204

    # 8. Verify Audit Logs captured every lifecycle transition without exposing secrets
    audit_resp = await async_client.get(
        f"/api/v1/admin/credentials/audit-logs?credential_id={cred_id}",
        headers=ADMIN_HEADERS,
    )
    assert audit_resp.status_code == 200
    actions = [entry["action"] for entry in audit_resp.json()]
    assert "CREATE_CREDENTIAL" in actions
    assert "DISABLE_CREDENTIAL" in actions
    assert "ENABLE_CREDENTIAL" in actions
    assert "UPDATE_CREDENTIAL" in actions
    assert "VALIDATE_CREDENTIAL_INVALID" in actions
    assert "REVOKE_CREDENTIAL" in actions
    assert "DELETE_CREDENTIAL" in actions
    assert raw_secret not in audit_resp.text


# =============================================================================
# 3. Selection Algorithms (Round-Robin, Weighted, Least-Active, LRU, Rate-Limit-Aware)
# =============================================================================
async def test_credential_selection_algorithms(test_app: FastAPI) -> None:
    cm: CredentialManager = test_app.state.credential_manager

    c1 = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="openai",
            label="Key 1",
            secret=SecretStr("sk-proj-key1_aaaaaaaaaaaa"),
            weight=3,
            max_concurrency=10,
        )
    )
    c2 = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="openai",
            label="Key 2",
            secret=SecretStr("sk-proj-key2_bbbbbbbbbbbb"),
            weight=1,
            max_concurrency=10,
        )
    )

    # A. ROUND_ROBIN alternates evenly
    rr_ids: list[str] = []
    for _ in range(4):
        lease = await cm.select_and_reserve_credential(
            "openai", strategy=SelectionStrategy.ROUND_ROBIN
        )
        rr_ids.append(lease.credential.credential_id)
        await cm.quota_manager.release_lease(
            request_id=lease.request_id, credential_id=lease.credential.credential_id
        )
    assert rr_ids.count(c1.id) == 2
    assert rr_ids.count(c2.id) == 2
    assert rr_ids[0] != rr_ids[1]

    # B. WEIGHTED (3:1 weight ratio across 4 selections -> 3 for Key 1, 1 for Key 2)
    w_ids: list[str] = []
    for _ in range(4):
        lease = await cm.select_and_reserve_credential(
            "openai", strategy=SelectionStrategy.WEIGHTED
        )
        w_ids.append(lease.credential.credential_id)
        await cm.quota_manager.release_lease(
            request_id=lease.request_id, credential_id=lease.credential.credential_id
        )
    assert w_ids.count(c1.id) == 3
    assert w_ids.count(c2.id) == 1

    # C. LEAST_ACTIVE picks the credential with fewer in-flight reservations
    await cm.quota_manager.reserve_lease(
        request_id="inflight-hold-1", credential_id=c1.id
    )
    await cm.quota_manager.reserve_lease(
        request_id="inflight-hold-2", credential_id=c1.id
    )
    la_lease = await cm.select_and_reserve_credential(
        "openai", strategy=SelectionStrategy.LEAST_ACTIVE
    )
    assert la_lease.credential.credential_id == c2.id
    await cm.quota_manager.release_lease(
        request_id=la_lease.request_id, credential_id=c2.id
    )
    await cm.quota_manager.release_lease(
        request_id="inflight-hold-1", credential_id=c1.id
    )
    await cm.quota_manager.release_lease(
        request_id="inflight-hold-2", credential_id=c1.id
    )

    # D. LEAST_RECENTLY_USED picks the credential with the older last_used_ms
    await cm._redis_manager.client.hset(
        f"ugw:cred:{c1.id}:state", mapping={"last_used_ms": "1000"}
    )
    await cm._redis_manager.client.hset(
        f"ugw:cred:{c2.id}:state", mapping={"last_used_ms": "9000"}
    )
    lru_lease = await cm.select_and_reserve_credential(
        "openai", strategy=SelectionStrategy.LEAST_RECENTLY_USED
    )
    assert lru_lease.credential.credential_id == c1.id
    await cm.quota_manager.release_lease(
        request_id=lru_lease.request_id, credential_id=c1.id
    )

    # E. RATE_LIMIT_AWARE picks the credential with higher remaining_requests
    await cm._redis_manager.client.hset(
        f"ugw:cred:{c1.id}:state", mapping={"remaining_requests": "5"}
    )
    await cm._redis_manager.client.hset(
        f"ugw:cred:{c2.id}:state", mapping={"remaining_requests": "4500"}
    )
    rla_lease = await cm.select_and_reserve_credential(
        "openai", strategy=SelectionStrategy.RATE_LIMIT_AWARE
    )
    assert rla_lease.credential.credential_id == c2.id
    await cm.quota_manager.release_lease(
        request_id=rla_lease.request_id, credential_id=c2.id
    )


# =============================================================================
# 4. Shared AccountGroup Quota Realism & Concurrency Tests
# =============================================================================
async def test_shared_account_group_quota_and_cooldown_coordination(
    test_app: FastAPI,
) -> None:
    cm: CredentialManager = test_app.state.credential_manager

    # Create two AccountGroups: Org A (shared RPM=2, max_concurrency=1) and Org B
    org_a = await cm.create_account_group(
        AccountGroupCreateRequest(
            provider_id="anthropic",
            name="Anthropic Workspace A",
            workspace_id="ws-alpha",
            max_concurrency=1,
            quota_config=QuotaConfigSpec(requests_per_minute=2),
        )
    )
    org_b = await cm.create_account_group(
        AccountGroupCreateRequest(
            provider_id="anthropic",
            name="Anthropic Workspace B",
            workspace_id="ws-beta",
            max_concurrency=5,
            quota_config=QuotaConfigSpec(requests_per_minute=100),
        )
    )

    # Two sibling keys in Workspace A, one key in Workspace B
    key_a1 = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="anthropic",
            label="WS-A Key 1",
            secret=SecretStr("sk-ant-wsA-key1-123456"),
            account_group_id=org_a.id,
            workspace_id="ws-alpha",
        )
    )
    key_a2 = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="anthropic",
            label="WS-A Key 2",
            secret=SecretStr("sk-ant-wsA-key2-123456"),
            account_group_id=org_a.id,
            workspace_id="ws-alpha",
        )
    )
    key_b1 = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="anthropic",
            label="WS-B Key 1",
            secret=SecretStr("sk-ant-wsB-key1-123456"),
            account_group_id=org_b.id,
            workspace_id="ws-beta",
        )
    )

    # 1. When key_a1 holds the single concurrency slot of Workspace A, key_a2 is ALSO blocked by shared concurrency!
    lease_1 = await cm.select_and_reserve_credential(
        "anthropic",
        excluded_credential_ids={key_b1.id},
    )
    assert lease_1.credential.credential_id in (key_a1.id, key_a2.id)

    health_a2 = await cm.get_credential_health(key_a2.id)
    assert health_a2.account_active_requests == 1
    assert health_a2.is_eligible is False

    # Selecting while Workspace A is at max_concurrency automatically routes to Workspace B (key_b1)
    lease_2 = await cm.select_and_reserve_credential("anthropic")
    assert lease_2.credential.credential_id == key_b1.id

    await cm.quota_manager.release_lease(
        request_id=lease_1.request_id,
        credential_id=lease_1.credential.credential_id,
        account_group_id=org_a.id,
    )
    await cm.quota_manager.release_lease(
        request_id=lease_2.request_id,
        credential_id=lease_2.credential.credential_id,
        account_group_id=org_b.id,
    )

    # 2. When Workspace A hits an account/workspace-scoped 429 cooldown, BOTH key_a1 and key_a2 enter COOLING_DOWN
    await cm.quota_manager.set_cooldown(
        provider_id="anthropic",
        supported_scopes=[QuotaScopeType.WORKSPACE, QuotaScopeType.CREDENTIAL],
        scope=QuotaScopeType.WORKSPACE,
        duration_seconds=30.0,
        credential_id=key_a1.id,
        account_group_id=org_a.id,
        workspace_id="ws-alpha",
    )
    h_a1 = await cm.get_credential_health(key_a1.id)
    h_a2 = await cm.get_credential_health(key_a2.id)
    assert h_a1.effective_status == CredentialStatus.COOLING_DOWN
    assert h_a2.effective_status == CredentialStatus.COOLING_DOWN
    assert h_a2.cooldown_scope == QuotaScopeType.WORKSPACE


# =============================================================================
# 5. Retry Coordinator & Failure Simulation Tests (429, 5xx, 401, 400)
# =============================================================================
async def test_execute_with_rotation_handles_429_401_5xx_and_non_retryable_400(
    test_app: FastAPI,
) -> None:
    cm: CredentialManager = test_app.state.credential_manager

    k_bad_auth = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="groq",
            label="Groq Invalid Key",
            secret=SecretStr("gsk_invalid_key_111111111"),
            weight=500,
        )
    )
    k_rate_limited = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="groq",
            label="Groq Throttled Key",
            secret=SecretStr("gsk_throttled_key_2222222"),
            weight=300,
        )
    )
    k_healthy = await cm.create_credential(
        CredentialCreateRequest(
            provider_id="groq",
            label="Groq Healthy Key",
            secret=SecretStr("gsk_healthy_key_333333333"),
            weight=100,
        )
    )

    call_log: list[str] = []

    async def simulated_upstream_call(cred) -> str:
        call_log.append(cred.credential_id)
        if cred.credential_id == k_bad_auth.id:
            raise UpstreamProviderError(
                "Invalid API Key",
                status_code=401,
                error_type="authentication_error",
                error_code="invalid_api_key",
                retryable=False,
                details={
                    "provider_id": "groq",
                    "quota_scope": "CREDENTIAL",
                    "requires_credential_disable": True,
                },
            )
        if cred.credential_id == k_rate_limited.id:
            raise UpstreamProviderError(
                "Rate limit reached",
                status_code=429,
                error_type="rate_limit_error",
                error_code="rate_limit_exceeded",
                retryable=True,
                details={
                    "provider_id": "groq",
                    "quota_scope": "CREDENTIAL",
                    "retry_after_seconds": 0.05,
                },
            )
        return "Success on healthy key!"

    result, winning_lease = await cm.execute_with_rotation(
        "groq",
        simulated_upstream_call,
        strategy=SelectionStrategy.RATE_LIMIT_AWARE,
        model="llama-3.3-70b-versatile",
    )
    assert result == "Success on healthy key!"
    assert winning_lease.credential.credential_id == k_healthy.id
    assert len(call_log) == 3

    # Verify k_bad_auth was automatically transitioned to INVALID
    bad_view = await cm.get_credential(k_bad_auth.id)
    assert bad_view.status == CredentialStatus.INVALID

    # Verify permanent 400 Invalid Request is NOT retried unnecessarily
    attempts_400 = 0

    async def invalid_request_call(_cred) -> str:
        nonlocal attempts_400
        attempts_400 += 1
        raise UpstreamProviderError(
            "Invalid temperature value",
            status_code=400,
            error_type="invalid_request_error",
            error_code="invalid_parameter",
            retryable=False,
        )

    with pytest.raises(UpstreamProviderError, match="Invalid temperature value"):
        await cm.execute_with_rotation("groq", invalid_request_call)
    assert attempts_400 == 1

