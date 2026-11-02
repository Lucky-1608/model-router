/**
 * Typed HTTP client for the Universal AI Gateway Admin & Observability API.
 * Strictly separates live backend responses from explicit Demo Fallback data.
 */

import {
  AdminRole,
  AnalyticsOverview,
  CircuitBreakerView,
  ClientApiKeyView,
  CredentialPublicView,
  MCPServerView,
  ModelAliasView,
  ModelMetadataView,
  ProviderSummary,
  RoutingPolicyView,
  SystemSettingsView,
  ToolSpecView,
  UsageRecordView,
} from "./types";

const DEFAULT_API_BASE =
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000";

export class GatewayApiClient {
  constructor(
    private readonly baseUrl: string = DEFAULT_API_BASE,
    private readonly adminToken: string = "",
    private readonly adminRole: AdminRole = "SUPER_ADMIN"
  ) {}

  private headers(): HeadersInit {
    const h: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Gateway-Admin-Role": this.adminRole,
    };
    if (this.adminToken) {
      h["X-Gateway-Admin-Token"] = this.adminToken;
    }
    return h;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        ...this.headers(),
        ...(init?.headers || {}),
      },
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`HTTP ${res.status}: ${text}`);
    }
    return (await res.json()) as T;
  }

  async fetchOverview(): Promise<AnalyticsOverview> {
    return this.request<AnalyticsOverview>("/api/v1/analytics/overview");
  }

  async fetchProviders(): Promise<ProviderSummary[]> {
    const res = await this.request<{ data: ProviderSummary[] }>(
      "/api/v1/providers"
    );
    return res.data;
  }

  async fetchCredentials(): Promise<CredentialPublicView[]> {
    return this.request<CredentialPublicView[]>("/api/v1/admin/credentials");
  }

  async createCredential(payload: {
    provider_id: string;
    label: string;
    secret: string;
    weight: number;
  }): Promise<CredentialPublicView> {
    return this.request<CredentialPublicView>("/api/v1/admin/credentials", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async toggleCredentialStatus(
    credentialId: string,
    action: "enable" | "disable" | "revoke"
  ): Promise<CredentialPublicView> {
    return this.request<CredentialPublicView>(
      `/api/v1/admin/credentials/${credentialId}/${action}`,
      { method: "POST" }
    );
  }

  async fetchModels(): Promise<ModelMetadataView[]> {
    const res = await this.request<{ data: ModelMetadataView[] }>(
      "/api/v1/routing/models"
    );
    return res.data;
  }

  async fetchAliases(): Promise<ModelAliasView[]> {
    const res = await this.request<{ data: ModelAliasView[] }>(
      "/api/v1/routing/aliases"
    );
    return res.data;
  }

  async fetchPolicies(): Promise<RoutingPolicyView[]> {
    const res = await this.request<{ data: RoutingPolicyView[] }>(
      "/api/v1/routing/policies"
    );
    return res.data;
  }

  async simulateRouting(payload: {
    requested_model: string;
    policy_name?: string;
    required_capabilities?: string[];
  }): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("/api/v1/routing/simulate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async fetchClientKeys(): Promise<ClientApiKeyView[]> {
    return this.request<ClientApiKeyView[]>("/api/v1/admin/client-keys");
  }

  async createClientKey(payload: {
    name: string;
    role: string;
    rate_limit_rpm: number;
    monthly_budget_usd?: number | null;
  }): Promise<ClientApiKeyView> {
    return this.request<ClientApiKeyView>("/api/v1/admin/client-keys", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async revokeClientKey(keyId: string): Promise<ClientApiKeyView> {
    return this.request<ClientApiKeyView>(
      `/api/v1/admin/client-keys/${keyId}/revoke`,
      { method: "POST" }
    );
  }

  async fetchRequestLogs(limit = 50): Promise<UsageRecordView[]> {
    return this.request<UsageRecordView[]>(
      `/api/v1/analytics/requests?limit=${limit}`
    );
  }

  async fetchCircuitBreakers(): Promise<CircuitBreakerView[]> {
    const res = await this.request<{ data: CircuitBreakerView[] }>(
      "/api/v1/analytics/circuit-breakers"
    );
    return res.data;
  }

  async resetCircuitBreaker(
    scope: string,
    targetId: string
  ): Promise<CircuitBreakerView> {
    return this.request<CircuitBreakerView>(
      `/api/v1/analytics/circuit-breakers/${scope}/${encodeURIComponent(
        targetId
      )}/reset`,
      { method: "POST" }
    );
  }

  async fetchTools(): Promise<ToolSpecView[]> {
    const res = await this.request<{ data: ToolSpecView[] }>("/api/v1/tools");
    return res.data;
  }

  async fetchMcpServers(): Promise<MCPServerView[]> {
    const res = await this.request<{ data: MCPServerView[] }>(
      "/api/v1/mcp/servers"
    );
    return res.data;
  }

  async fetchSystemSettings(): Promise<SystemSettingsView> {
    return this.request<SystemSettingsView>("/api/v1/admin/system/settings");
  }
}

