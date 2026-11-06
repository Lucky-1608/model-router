"""Tests for Phase 7 (Prompt 10): Admin Control Plane API, Client Gateway API Keys, and Dashboard Summary."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from gateway.config.settings import GatewaySettings
from gateway.main import create_app

TEST_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


@pytest.fixture
def phase7_settings(tmp_path: Path) -> GatewaySettings:
    db_file = tmp_path / "test_phase7.db"
    return GatewaySettings(
        app_name="Universal AI Gateway Phase 7 Test",
        env="test",
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        redis_url="memory://",
        master_kek_base64=TEST_MASTER_KEY,
        admin_bootstrap_token="phase7-admin-secret-token",
    )


def test_admin_client_api_key_lifecycle_rbac_and_hash_only_storage(
    phase7_settings: GatewaySettings,
) -> None:
    """Verifies client API key creation returns raw key once, subsequent listings redact it, and RBAC is enforced."""
    app = create_app(phase7_settings)
    with TestClient(app) as client:
        # 1. Unauthenticated request rejected with 401
        unauth = client.get("/api/v1/admin/client-keys")
        assert unauth.status_code == 401

        # 2. READ_ONLY role cannot create keys (403)
        forbidden = client.post(
            "/api/v1/admin/client-keys",
            headers={
                "X-Gateway-Admin-Token": "phase7-admin-secret-token",
                "X-Gateway-Admin-Role": "READ_ONLY",
            },
            json={
                "name": "Readonly Attempt",
                "role": "CLIENT",
                "rate_limit_rpm": 300,
            },
        )
        assert forbidden.status_code == 403

        # 3. SUPER_ADMIN creates a new client key; raw api_key is returned once
        create_resp = client.post(
            "/api/v1/admin/client-keys",
            headers={
                "X-Gateway-Admin-Token": "phase7-admin-secret-token",
                "X-Gateway-Admin-Role": "SUPER_ADMIN",
            },
            json={
                "name": "Production Backend Service",
                "role": "CLIENT",
                "allowed_providers": ["openai", "anthropic"],
                "allowed_tools": ["gateway_calculator"],
                "rate_limit_rpm": 1200,
                "monthly_budget_usd": 250.0,
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        raw_key = created["api_key"]
        key_id = created["id"]
        assert raw_key.startswith("ugw_live_")
        assert created["key_prefix"].startswith("ugw_live_")
        assert created["enabled"] is True

        # 4. Subsequent GET /api/v1/admin/client-keys never returns raw api_key or key_hash
        list_resp = client.get(
            "/api/v1/admin/client-keys",
            headers={
                "X-Gateway-Admin-Token": "phase7-admin-secret-token",
                "X-Gateway-Admin-Role": "READ_ONLY",
            },
        )
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert len(items) == 1
        assert "api_key" not in items[0]
        assert "key_hash" not in items[0]
        assert raw_key not in list_resp.text

        # 5. Update client key (name, RPM, role, budget)
        update_resp = client.put(
            f"/api/v1/admin/client-keys/{key_id}",
            headers={"X-Gateway-Admin-Token": "phase7-admin-secret-token"},
            json={
                "name": "Production Backend Service Updated",
                "rate_limit_rpm": 2400,
                "monthly_budget_usd": 500.0,
            },
        )
        assert update_resp.status_code == 200
        updated = update_resp.json()
        assert updated["name"] == "Production Backend Service Updated"
        assert updated["rate_limit_rpm"] == 2400
        assert updated["monthly_budget_usd"] == 500.0

        # 6. Revoke and delete key
        revoke_resp = client.post(
            f"/api/v1/admin/client-keys/{key_id}/revoke",
            headers={"X-Gateway-Admin-Token": "phase7-admin-secret-token"},
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["enabled"] is False

        del_resp = client.delete(
            f"/api/v1/admin/client-keys/{key_id}",
            headers={"X-Gateway-Admin-Token": "phase7-admin-secret-token"},
        )
        assert del_resp.status_code == 204


def test_admin_system_settings_and_dashboard_summary_endpoints(
    phase7_settings: GatewaySettings,
) -> None:
    """Verifies /api/v1/admin/system/settings and /api/v1/admin/dashboard/summary return sanitized telemetry."""
    app = create_app(phase7_settings)
    with TestClient(app) as client:
        headers = {"X-Gateway-Admin-Token": "phase7-admin-secret-token"}

        settings_resp = client.get(
            "/api/v1/admin/system/settings", headers=headers
        )
        assert settings_resp.status_code == 200
        s_data = settings_resp.json()
        assert s_data["encryption_algorithm"] == "AES-256-GCM"
        assert s_data["secret_redaction_enabled"] is True
        assert s_data["ssrf_protection_enabled"] is True
        assert s_data["default_routing_weights"]["quality"] == 0.30
        assert TEST_MASTER_KEY not in settings_resp.text
        assert "phase7-admin-secret-token" not in settings_resp.text

        summary_resp = client.get(
            "/api/v1/admin/dashboard/summary", headers=headers
        )
        assert summary_resp.status_code == 200
        summary = summary_resp.json()
        assert summary["counts"]["providers"] >= 7
        assert summary["counts"]["aliases"] >= 10
        assert summary["counts"]["routing_policies"] >= 5
        assert summary["counts"]["tools"] >= 3


def test_nextjs_dashboard_structure_and_12_views_integrity() -> None:
    """Verifies the Next.js TypeScript dashboard files exist and implement all 12 required operational views."""
    repo_root = Path(__file__).resolve().parents[1]
    dashboard_dir = repo_root / "dashboard"

    required_files = [
        dashboard_dir / "package.json",
        dashboard_dir / "tsconfig.json",
        dashboard_dir / "tailwind.config.ts",
        dashboard_dir / "src" / "lib" / "types.ts",
        dashboard_dir / "src" / "lib" / "api.ts",
        dashboard_dir / "src" / "lib" / "mockData.ts",
        dashboard_dir / "src" / "app" / "layout.tsx",
        dashboard_dir / "src" / "app" / "page.tsx",
    ]
    for path in required_files:
        assert path.exists(), f"Missing dashboard file: {path}"

    page_source = (dashboard_dir / "src" / "app" / "page.tsx").read_text(
        encoding="utf-8"
    )
    expected_sections = [
        "overview",
        "providers",
        "credentials",
        "models",
        "routing_policies",
        "client_keys",
        "analytics",
        "request_logs",
        "tools",
        "mcp_servers",
        "custom_integrations",
        "system_settings",
    ]
    for sec in expected_sections:
        assert f'"{sec}"' in page_source, f"Dashboard missing section {sec}"

    # Verify explicit separation between LIVE_BACKEND and DEMO_FALLBACK
    assert "LIVE_BACKEND" in page_source
    assert "DEMO_FALLBACK" in page_source

