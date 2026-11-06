"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Copy,
  Check,
  Trash2,
  Edit3,
  X,
  Key,
  AlertTriangle,
  RefreshCw,
  PlusCircle,
  Globe,
  Server,
  Plug,
} from "lucide-react";
import { GatewayApiClient } from "@/lib/api";
import {
  AccountGroupView,
  AdminRole,
  AnalyticsOverview,
  CircuitBreakerView,
  ClientApiKeyView,
  CredentialPublicView,
  DashboardSectionId,
  DataSourceMode,
  MCPServerView,
  ModelAliasView,
  ModelMetadataView,
  ProviderSummary,
  RoutingPolicyView,
  SystemSettingsView,
  ToolSpecView,
  UsageRecordView,
} from "@/lib/types";

const NAV_ITEMS: { id: DashboardSectionId; label: string; badge?: string }[] = [
  { id: "overview", label: "1. Overview & Health" },
  { id: "providers", label: "2. Providers & Capabilities" },
  { id: "credentials", label: "3. Credentials & Quotas" },
  { id: "models", label: "4. Models & Aliases" },
  { id: "routing_policies", label: "5. Routing Policies" },
  { id: "client_keys", label: "6. Client API Keys" },
  { id: "analytics", label: "7. Usage & Cost Analytics" },
  { id: "request_logs", label: "8. Request & Trace Logs" },
  { id: "tools", label: "9. Tool Registry" },
  { id: "mcp_servers", label: "10. MCP Connections" },
  { id: "custom_integrations", label: "11. Custom Integrations" },
  { id: "system_settings", label: "12. System Settings" },
];

const EMPTY_OVERVIEW: AnalyticsOverview = {
  total_requests: 0,
  successful_requests: 0,
  failed_requests: 0,
  success_rate: 1.0,
  fallback_requests: 0,
  fallback_rate: 0.0,
  total_input_tokens: 0,
  total_output_tokens: 0,
  total_tokens: 0,
  total_characters: 0,
  total_estimated_cost_usd: 0.0,
  avg_latency_ms: 0,
  avg_ttft_ms: null,
};

/**
 * Deterministic number formatting using fixed 'en-US' locale to prevent
 * SSR / browser hydration mismatches across different system locales.
 */
function formatNumber(val: number | null | undefined): string {
  if (val === null || val === undefined) return "0";
  return new Intl.NumberFormat("en-US").format(val);
}

export default function GatewayDashboardPage() {
  const [activeSection, setActiveSection] =
    useState<DashboardSectionId>("overview");
  const [gatewayUrl, setGatewayUrl] = useState<string>(
    process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000"
  );
  const [adminToken, setAdminToken] = useState<string>(
    process.env.NEXT_PUBLIC_ADMIN_TOKEN || "ugw_admin_dev_bootstrap_replace_me_in_prod"
  );
  const [adminRole, setAdminRole] = useState<AdminRole>("SUPER_ADMIN");
  const [dataSource, setDataSource] = useState<DataSourceMode>("LIVE_BACKEND");
  const [statusBanner, setStatusBanner] = useState<string>(
    "Connecting to Universal AI Gateway..."
  );

  // State across all 12 operational domains — all initialized empty from live API
  const [overview, setOverview] = useState<AnalyticsOverview>(EMPTY_OVERVIEW);
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [credentials, setCredentials] = useState<CredentialPublicView[]>([]);
  const [accountGroups, setAccountGroups] = useState<AccountGroupView[]>([]);
  const [models, setModels] = useState<ModelMetadataView[]>([]);
  const [aliases, setAliases] = useState<ModelAliasView[]>([]);
  const [policies, setPolicies] = useState<RoutingPolicyView[]>([]);
  const [clientKeys, setClientKeys] = useState<ClientApiKeyView[]>([]);
  const [requestLogs, setRequestLogs] = useState<UsageRecordView[]>([]);
  const [tools, setTools] = useState<ToolSpecView[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServerView[]>([]);
  const [circuits, setCircuits] = useState<CircuitBreakerView[]>([]);
  const [systemSettings, setSystemSettings] = useState<SystemSettingsView | null>(null);

  // Interactive form states
  const [simAlias, setSimAlias] = useState<string>("coding");
  const [simPolicy, setSimPolicy] = useState<string>("balanced");
  const [simTraceResult, setSimTraceResult] = useState<string | null>(null);

  const [newKeyName, setNewKeyName] = useState<string>("");
  const [newKeyRpm, setNewKeyRpm] = useState<number>(600);
  const [newlyIssuedRawKey, setNewlyIssuedRawKey] = useState<string | null>(
    null
  );
  const [copiedKey, setCopiedKey] = useState<boolean>(false);

  // Client API key editing states
  const [editingClientKey, setEditingClientKey] = useState<ClientApiKeyView | null>(null);
  const [editKeyName, setEditKeyName] = useState<string>("");
  const [editKeyRole, setEditKeyRole] = useState<string>("CLIENT");
  const [editKeyRpm, setEditKeyRpm] = useState<number>(600);
  const [editKeyBudget, setEditKeyBudget] = useState<string>("");
  const [editKeyEnabled, setEditKeyEnabled] = useState<boolean>(true);

  const [newCredProvider, setNewCredProvider] = useState<string>("openai");
  const [newCredLabel, setNewCredLabel] = useState<string>("");
  const [newCredSecret, setNewCredSecret] = useState<string>("");

  // MCP Connection form states
  const [newMcpId, setNewMcpId] = useState<string>("");
  const [newMcpName, setNewMcpName] = useState<string>("");
  const [newMcpTransport, setNewMcpTransport] = useState<string>("http_jsonrpc");
  const [newMcpUrl, setNewMcpUrl] = useState<string>("");
  const [newMcpTimeout, setNewMcpTimeout] = useState<number>(15);
  const [syncingMcpId, setSyncingMcpId] = useState<string | null>(null);

  // Custom Integration form states
  const [newCustomProvId, setNewCustomProvId] = useState<string>("");
  const [newCustomProvName, setNewCustomProvName] = useState<string>("");
  const [newCustomProvBaseUrl, setNewCustomProvBaseUrl] = useState<string>("");
  const [newCustomProvAllowLocal, setNewCustomProvAllowLocal] = useState<boolean>(true);

  const apiClient = useMemo(
    () => new GatewayApiClient(gatewayUrl, adminToken, adminRole),
    [gatewayUrl, adminToken, adminRole]
  );

  const canMutate = adminRole !== "READ_ONLY";

  const syncLiveBackend = useCallback(async () => {
    try {
      const [
        liveOverview,
        liveProviders,
        liveModels,
        liveAliases,
        livePolicies,
        liveLogs,
        liveTools,
        liveMcp,
        liveCircuits,
      ] = await Promise.all([
        apiClient.fetchOverview(),
        apiClient.fetchProviders(),
        apiClient.fetchModels(),
        apiClient.fetchAliases(),
        apiClient.fetchPolicies(),
        apiClient.fetchRequestLogs(50),
        apiClient.fetchTools(),
        apiClient.fetchMcpServers(),
        apiClient.fetchCircuitBreakers(),
      ]);

      setOverview(liveOverview ?? EMPTY_OVERVIEW);
      setProviders(Array.isArray(liveProviders) ? liveProviders : []);
      setModels(Array.isArray(liveModels) ? liveModels : []);
      setAliases(Array.isArray(liveAliases) ? liveAliases : []);
      setPolicies(Array.isArray(livePolicies) ? livePolicies : []);
      setRequestLogs(Array.isArray(liveLogs) ? liveLogs : []);
      setTools(Array.isArray(liveTools) ? liveTools : []);
      setMcpServers(Array.isArray(liveMcp) ? liveMcp : []);
      setCircuits(Array.isArray(liveCircuits) ? liveCircuits : []);

      try {
        const [liveCreds, liveClientKeys, liveSettings, liveAccountGroups] = await Promise.all([
          apiClient.fetchCredentials(),
          apiClient.fetchClientKeys(),
          apiClient.fetchSystemSettings(),
          apiClient.fetchAccountGroups(),
        ]);
        setCredentials(Array.isArray(liveCreds) ? liveCreds : []);
        setClientKeys(Array.isArray(liveClientKeys) ? liveClientKeys : []);
        if (liveSettings) setSystemSettings(liveSettings);
        setAccountGroups(Array.isArray(liveAccountGroups) ? liveAccountGroups : []);
      } catch (adminErr) {
        console.warn("Admin endpoints error:", adminErr);
      }

      setDataSource("LIVE_BACKEND");
      setStatusBanner(
        `Connected to Live Gateway at ${gatewayUrl} (Role: ${adminRole})`
      );
    } catch (err) {
      setDataSource("DEMO_FALLBACK");
      setStatusBanner(
        `Unable to reach Gateway backend at ${gatewayUrl} (${
          err instanceof Error ? err.message : "connection refused"
        }). Ensure the gateway is running on port 8000.`
      );
    }
  }, [apiClient, gatewayUrl, adminRole]);

  useEffect(() => {
    void syncLiveBackend();
  }, [syncLiveBackend]);

  const handleSimulateRoute = async () => {
    try {
      const trace = await apiClient.simulateRouting({
        requested_model: simAlias,
        policy_name: simPolicy,
      });
      setSimTraceResult(JSON.stringify(trace, null, 2));
    } catch (e) {
      setSimTraceResult(
        `Simulation error: ${e instanceof Error ? e.message : String(e)}`
      );
    }
  };

  const handleCreateClientKey = async () => {
    if (!canMutate || !newKeyName.trim()) return;
    try {
      if (dataSource === "LIVE_BACKEND") {
        const created = await apiClient.createClientKey({
          name: newKeyName.trim(),
          role: "CLIENT",
          rate_limit_rpm: newKeyRpm,
          monthly_budget_usd: 100,
        });
        setNewlyIssuedRawKey(created.api_key || null);
        setCopiedKey(false);
        setClientKeys((prev) => [created, ...(prev || [])]);
        setNewKeyName("");
        setStatusBanner(`Created live API client key: ${created.name}`);
        return;
      }
      throw new Error("Demo fallback mode");
    } catch {
      const fakeRawKey = `ugw_live_${Math.random().toString(36).substring(2, 10)}${Math.random().toString(36).substring(2, 10)}`;
      const newKeyView: ClientApiKeyView = {
        id: `key-${Date.now()}`,
        name: newKeyName.trim(),
        key_prefix: fakeRawKey.substring(0, 12),
        role: "CLIENT",
        allowed_providers: [],
        allowed_tools: [],
        rate_limit_rpm: newKeyRpm,
        monthly_budget_usd: 100,
        enabled: true,
        created_at: new Date().toISOString(),
        api_key: fakeRawKey,
      };
      setNewlyIssuedRawKey(fakeRawKey);
      setCopiedKey(false);
      setClientKeys((prev) => [newKeyView, ...(prev || [])]);
      setNewKeyName("");
      setStatusBanner(`Created client API key: ${newKeyView.name}`);
    }
  };

  const handleCopyRawKey = async () => {
    if (!newlyIssuedRawKey) return;
    try {
      await navigator.clipboard.writeText(newlyIssuedRawKey);
      setCopiedKey(true);
      setTimeout(() => setCopiedKey(false), 2500);
    } catch (err) {
      console.error("Clipboard copy failed", err);
    }
  };

  const handleOpenEditKey = (key: ClientApiKeyView) => {
    setEditingClientKey(key);
    setEditKeyName(key.name);
    setEditKeyRole(key.role);
    setEditKeyRpm(key.rate_limit_rpm);
    setEditKeyBudget(
      key.monthly_budget_usd !== null && key.monthly_budget_usd !== undefined
        ? String(key.monthly_budget_usd)
        : ""
    );
    setEditKeyEnabled(key.enabled);
  };

  const handleSaveEditKey = async () => {
    if (!editingClientKey) return;
    const payload = {
      name: editKeyName.trim() || editingClientKey.name,
      role: editKeyRole,
      rate_limit_rpm: editKeyRpm,
      monthly_budget_usd: editKeyBudget ? parseFloat(editKeyBudget) : null,
      enabled: editKeyEnabled,
    };
    try {
      if (dataSource === "LIVE_BACKEND") {
        const updated = await apiClient.updateClientKey(
          editingClientKey.id,
          payload
        );
        setClientKeys((prev) =>
          prev.map((k) => (k.id === updated.id ? { ...k, ...updated } : k))
        );
        setStatusBanner(`Updated client API key: ${updated.name}`);
      } else {
        setClientKeys((prev) =>
          prev.map((k) =>
            k.id === editingClientKey.id ? { ...k, ...payload } : k
          )
        );
        setStatusBanner(`Updated client API key: ${payload.name}`);
      }
      setEditingClientKey(null);
    } catch (err) {
      if (dataSource === "DEMO_FALLBACK" || String(err).includes("fetch")) {
        setClientKeys((prev) =>
          prev.map((k) =>
            k.id === editingClientKey.id ? { ...k, ...payload } : k
          )
        );
        setEditingClientKey(null);
        setStatusBanner(`Updated client API key (Local): ${payload.name}`);
        return;
      }
      alert(
        `Failed to update client key: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const handleDeleteClientKey = async (key: ClientApiKeyView) => {
    if (!canMutate) return;
    if (
      !window.confirm(
        `Are you sure you want to permanently delete the API key "${key.name}" (${key.key_prefix})? This action cannot be undone.`
      )
    ) {
      return;
    }
    try {
      if (dataSource === "LIVE_BACKEND") {
        await apiClient.deleteClientKey(key.id);
      }
      setClientKeys((prev) => prev.filter((k) => k.id !== key.id));
      setStatusBanner(`Deleted client API key: ${key.name}`);
    } catch (err) {
      if (dataSource === "DEMO_FALLBACK" || String(err).includes("fetch")) {
        setClientKeys((prev) => prev.filter((k) => k.id !== key.id));
        setStatusBanner(`Deleted client API key: ${key.name}`);
        return;
      }
      alert(
        `Failed to delete client key: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const handleToggleClientKey = async (key: ClientApiKeyView) => {
    if (!canMutate) return;
    const nextEnabled = !key.enabled;
    try {
      if (dataSource === "LIVE_BACKEND") {
        if (key.enabled) {
          await apiClient.revokeClientKey(key.id);
        } else {
          await apiClient.updateClientKey(key.id, { enabled: true });
        }
      }
      setClientKeys((prev) =>
        prev.map((k) => (k.id === key.id ? { ...k, enabled: nextEnabled } : k))
      );
      setStatusBanner(
        `${nextEnabled ? "Re-enabled" : "Revoked"} client API key: ${key.name}`
      );
    } catch (err) {
      if (dataSource === "DEMO_FALLBACK" || String(err).includes("fetch")) {
        setClientKeys((prev) =>
          prev.map((k) =>
            k.id === key.id ? { ...k, enabled: nextEnabled } : k
          )
        );
        setStatusBanner(
          `${nextEnabled ? "Re-enabled" : "Revoked"} client API key: ${key.name}`
        );
        return;
      }
      alert(
        `Failed to toggle key status: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const handleCreateCredential = async () => {
    if (!canMutate || !newCredLabel.trim() || !newCredSecret.trim()) return;
    try {
      const created = await apiClient.createCredential({
        provider_id: newCredProvider,
        label: newCredLabel.trim(),
        secret: newCredSecret.trim(),
        weight: 100,
      });
      setCredentials((prev) => [created, ...(prev || [])]);
      setNewCredLabel("");
      setNewCredSecret("");
      setStatusBanner(`Stored encrypted credential: ${created.label}`);
    } catch (err) {
      alert(
        `Failed to store encrypted credential: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  // --- MCP Server Handlers ---
  const handleRegisterMcpServer = async () => {
    if (!canMutate || !newMcpId.trim() || !newMcpName.trim()) return;
    try {
      const created = await apiClient.registerMcpServer({
        server_id: newMcpId.trim(),
        name: newMcpName.trim(),
        transport_type: newMcpTransport,
        endpoint_url: newMcpUrl.trim() || undefined,
        timeout_seconds: newMcpTimeout,
        allow_local_network: true,
      });
      setMcpServers((prev) => [created, ...(prev || [])]);
      setNewMcpId("");
      setNewMcpName("");
      setNewMcpUrl("");
      setStatusBanner(`Registered MCP Server: ${created.name}`);
    } catch (err) {
      alert(
        `Failed to register MCP server: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const handleSyncMcpTools = async (serverId: string) => {
    setSyncingMcpId(serverId);
    try {
      const res = await apiClient.syncMcpServerTools(serverId);
      const [updatedServers, updatedTools] = await Promise.all([
        apiClient.fetchMcpServers(),
        apiClient.fetchTools(),
      ]);
      setMcpServers(updatedServers);
      setTools(updatedTools);
      setStatusBanner(
        `Synced ${res.synced_count} tool(s) from MCP server "${serverId}"`
      );
    } catch (err) {
      alert(
        `Failed to sync tools from MCP server: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    } finally {
      setSyncingMcpId(null);
    }
  };

  const handleDeleteMcpServer = async (serverId: string) => {
    if (!canMutate) return;
    if (
      !window.confirm(
        `Are you sure you want to disconnect and delete MCP server "${serverId}"? Any tools synced from this server will be removed.`
      )
    ) {
      return;
    }
    try {
      await apiClient.unregisterMcpServer(serverId);
      setMcpServers((prev) => prev.filter((s) => s.server_id !== serverId));
      const updatedTools = await apiClient.fetchTools();
      setTools(updatedTools);
      setStatusBanner(`Disconnected MCP server: ${serverId}`);
    } catch (err) {
      alert(
        `Failed to delete MCP server: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  // --- Custom Provider Handlers ---
  const handleRegisterCustomProvider = async () => {
    if (
      !canMutate ||
      !newCustomProvId.trim() ||
      !newCustomProvName.trim() ||
      !newCustomProvBaseUrl.trim()
    ) {
      return;
    }
    try {
      const created = await apiClient.registerCustomProvider({
        provider_id: newCustomProvId.trim(),
        display_name: newCustomProvName.trim(),
        base_url: newCustomProvBaseUrl.trim(),
        adapter_type: "custom_http",
        allow_local_network: newCustomProvAllowLocal,
      });
      setProviders((prev) => [created, ...(prev || [])]);
      setNewCustomProvId("");
      setNewCustomProvName("");
      setNewCustomProvBaseUrl("");
      setStatusBanner(
        `Registered custom REST AI provider: ${created.display_name}`
      );
    } catch (err) {
      alert(
        `Failed to register custom provider: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const handleDeleteCustomProvider = async (providerId: string) => {
    if (!canMutate) return;
    if (
      !window.confirm(
        `Are you sure you want to unregister custom provider "${providerId}"?`
      )
    ) {
      return;
    }
    try {
      await apiClient.unregisterCustomProvider(providerId);
      setProviders((prev) => prev.filter((p) => p.provider_id !== providerId));
      setStatusBanner(`Unregistered custom provider: ${providerId}`);
    } catch (err) {
      alert(
        `Failed to unregister custom provider: ${
          err instanceof Error ? err.message : String(err)
        }`
      );
    }
  };

  const renderBadge = (state: string) => {
    const upper = state.toUpperCase();
    if (
      upper === "SUPPORTED" ||
      upper === "HEALTHY" ||
      upper === "ACTIVE" ||
      upper === "CLOSED"
    ) {
      return (
        <span className="px-2 py-0.5 text-xs font-medium rounded bg-emerald-950 text-emerald-300 border border-emerald-700">
          {state}
        </span>
      );
    }
    if (
      upper === "DEGRADED" ||
      upper === "COOLING_DOWN" ||
      upper === "HALF_OPEN" ||
      upper === "UNKNOWN"
    ) {
      return (
        <span className="px-2 py-0.5 text-xs font-medium rounded bg-amber-950 text-amber-300 border border-amber-700">
          {state}
        </span>
      );
    }
    return (
      <span className="px-2 py-0.5 text-xs font-medium rounded bg-rose-950 text-rose-300 border border-rose-700">
        {state}
      </span>
    );
  };

  return (
    <div className="flex min-h-screen bg-[#090D16] text-gray-100">
      {/* Left Navigation Sidebar */}
      <aside className="w-72 border-r border-gray-800 bg-[#0D1322] flex flex-col justify-between p-4">
        <div>
          <div className="mb-6 pb-4 border-b border-gray-800">
            <h1 className="text-lg font-bold tracking-tight text-white">
              Universal AI Gateway
            </h1>
            <p className="text-xs text-gray-400 mt-1">
              Multi-Provider Control Plane v0.1.0
            </p>
            <div className="mt-3 flex items-center gap-2">
              <span
                className={`px-2 py-0.5 text-xs font-semibold rounded ${
                  dataSource === "LIVE_BACKEND"
                    ? "bg-emerald-900/70 text-emerald-300 border border-emerald-600"
                    : "bg-amber-900/70 text-amber-200 border border-amber-600"
                }`}
              >
                {dataSource === "LIVE_BACKEND"
                  ? "LIVE API MODE"
                  : "DEMO / MOCK MODE"}
              </span>
              <span className="px-2 py-0.5 text-xs rounded bg-blue-950 text-blue-300 border border-blue-800">
                {adminRole}
              </span>
            </div>
          </div>

          <nav className="space-y-1">
            {NAV_ITEMS.map((item) => (
              <button
                key={item.id}
                onClick={() => setActiveSection(item.id)}
                className={`w-full text-left px-3 py-2 rounded-md text-sm font-medium transition ${
                  activeSection === item.id
                    ? "bg-blue-600 text-white"
                    : "text-gray-300 hover:bg-gray-800/70 hover:text-white"
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </div>

        {/* Role & Connection Controls */}
        <div className="pt-4 border-t border-gray-800 space-y-2 text-xs">
          <label className="block text-gray-400">
            Active RBAC Role
            <select
              value={adminRole}
              onChange={(e) => setAdminRole(e.target.value as AdminRole)}
              className="mt-1 w-full rounded bg-gray-900 border border-gray-700 px-2 py-1.5 text-gray-100"
            >
              <option value="SUPER_ADMIN">SUPER_ADMIN (Full Access)</option>
              <option value="OPERATOR">OPERATOR (Ops & Quotas)</option>
              <option value="READ_ONLY">READ_ONLY (Auditor View)</option>
            </select>
          </label>
          <button
            onClick={() => void syncLiveBackend()}
            className="w-full py-1.5 px-3 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium"
          >
            Sync Live Gateway
          </button>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 p-8 overflow-y-auto space-y-6">
        {/* Data Source Transparency Banner */}
        <div
          className={`p-3 rounded-lg border text-xs flex items-center justify-between ${
            dataSource === "LIVE_BACKEND"
              ? "bg-emerald-950/40 border-emerald-800 text-emerald-200"
              : "bg-amber-950/40 border-amber-800 text-amber-200"
          }`}
        >
          <span>{statusBanner}</span>
          <span className="font-mono">
            Secrets Redacted • AES-256-GCM Envelope Encryption
          </span>
        </div>

        {/* 1. OVERVIEW & HEALTH */}
        {activeSection === "overview" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              1. Gateway Overview & Circuit Health
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">Total Requests</div>
                <div className="text-2xl font-bold mt-1">
                  {formatNumber(overview.total_requests)}
                </div>
                <div className="text-xs text-emerald-400 mt-1">
                  {(overview.success_rate * 100).toFixed(2)}% Success Rate
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">
                  Fallback Executions
                </div>
                <div className="text-2xl font-bold mt-1">
                  {formatNumber(overview.fallback_requests)}
                </div>
                <div className="text-xs text-amber-400 mt-1">
                  {(overview.fallback_rate * 100).toFixed(2)}% of traffic
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">
                  Total Tokens Processed
                </div>
                <div className="text-2xl font-bold mt-1">
                  {formatNumber(overview.total_tokens)}
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  Est. Cost: ${overview.total_estimated_cost_usd.toFixed(4)}
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">
                  Avg Latency / Avg TTFT
                </div>
                <div className="text-2xl font-bold mt-1">
                  {overview.avg_latency_ms} ms
                </div>
                <div className="text-xs text-blue-400 mt-1">
                  TTFT:{" "}
                  {overview.avg_ttft_ms !== null
                    ? `${overview.avg_ttft_ms} ms`
                    : "N/A"}
                </div>
              </div>
            </div>

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <h3 className="text-base font-semibold mb-3">
                Active Circuit Breakers (Redis-Coordinated)
              </h3>
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Scope</th>
                    <th className="py-2">Target ID</th>
                    <th className="py-2">Circuit State</th>
                    <th className="py-2">Failures</th>
                    <th className="py-2">Health</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {circuits.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-6 text-center text-xs text-gray-500">
                        All circuits are healthy (or initializing). Circuit trips and health status are tracked here in real time.
                      </td>
                    </tr>
                  ) : (
                    circuits.map((c) => (
                      <tr key={`${c.scope}:${c.target_id}`}>
                        <td className="py-2 font-mono text-xs">{c.scope}</td>
                        <td className="py-2 font-medium">{c.target_id}</td>
                        <td className="py-2">{renderBadge(c.state)}</td>
                        <td className="py-2">{c.failure_count}</td>
                        <td className="py-2">{renderBadge(c.health_status)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 2. PROVIDERS & CAPABILITIES */}
        {activeSection === "providers" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              2. Provider Registry & Explicit Capability Manifests
            </h2>
            <p className="text-sm text-gray-400">
              Capabilities are strictly classified as SUPPORTED, UNSUPPORTED, or
              UNKNOWN. Missing capabilities are never fabricated.
            </p>
            <div className="grid grid-cols-1 gap-4">
              {(providers || []).map((p) => (
                <div
                  key={p.provider_id}
                  className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-3"
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <span className="text-lg font-semibold">
                        {p.display_name}
                      </span>
                      <span className="ml-2 text-xs font-mono text-gray-400">
                        ({p.provider_id}) • {p.base_url}
                      </span>
                    </div>
                    {renderBadge(p.health_status || "HEALTHY")}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(p.capabilities || {}).map(([cap, st]) => (
                      <div
                        key={cap}
                        className="flex items-center gap-1.5 bg-gray-900 px-2.5 py-1 rounded border border-gray-800 text-xs"
                      >
                        <span className="font-mono text-gray-300">{cap}:</span>
                        {renderBadge(st)}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 3. CREDENTIALS & QUOTAS */}
        {activeSection === "credentials" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              3. Multi-Key Credential Manager & Shared Account Quotas
            </h2>

            {canMutate && (
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800 space-y-3">
                <h3 className="text-sm font-semibold">
                  Register Encrypted Provider API Credential (AES-256-GCM)
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                  <select
                    value={newCredProvider}
                    onChange={(e) => setNewCredProvider(e.target.value)}
                    className="rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm"
                  >
                    {(providers || []).map((p) => (
                      <option key={p.provider_id} value={p.provider_id}>
                        {p.display_name}
                      </option>
                    ))}
                  </select>
                  <input
                    type="text"
                    placeholder="Credential Label (e.g. Prod Key #3)"
                    value={newCredLabel}
                    onChange={(e) => setNewCredLabel(e.target.value)}
                    className="rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm"
                  />
                  <input
                    type="password"
                    placeholder="Raw Provider Secret (Never displayed again)"
                    value={newCredSecret}
                    onChange={(e) => setNewCredSecret(e.target.value)}
                    className="rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm"
                  />
                  <button
                    onClick={() => void handleCreateCredential()}
                    className="rounded bg-blue-600 hover:bg-blue-500 px-4 py-2 text-sm font-medium"
                  >
                    Encrypt & Save Key
                  </button>
                </div>
              </div>
            )}

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <h3 className="text-base font-semibold mb-3">
                Authorized Provider Credentials (Secrets Masked)
              </h3>
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Provider</th>
                    <th className="py-2">Label</th>
                    <th className="py-2">Masked Hint</th>
                    <th className="py-2">Status</th>
                    <th className="py-2">Weight</th>
                    <th className="py-2">Shared Account Group</th>
                    <th className="py-2">RPM / TPM</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {credentials.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="py-6 text-center text-xs text-gray-500">
                        No credentials registered yet. Register your upstream provider API keys above or via POST /api/v1/admin/credentials.
                      </td>
                    </tr>
                  ) : (
                    credentials.map((c) => (
                      <tr key={c.id}>
                        <td className="py-2 font-mono">{c.provider_id}</td>
                        <td className="py-2 font-medium">{c.label}</td>
                        <td className="py-2 font-mono text-xs text-gray-400">
                          {c.masked_hint}
                        </td>
                        <td className="py-2">{renderBadge(c.status)}</td>
                        <td className="py-2">{c.weight}</td>
                        <td className="py-2 font-mono text-xs">
                          {c.account_group_id || "independent"}
                        </td>
                        <td className="py-2 text-xs">
                          {c.rpm_limit ?? "∞"} RPM / {c.tpm_limit ?? "∞"} TPM
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <h3 className="text-base font-semibold mb-3">
                Shared Account / Organization Quota Groups
              </h3>
              <p className="text-xs text-gray-400 mb-3">
                Multiple API keys belonging to the same provider organization
                share account-level rate limits and cooldowns.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {accountGroups.length === 0 ? (
                  <div className="col-span-2 p-6 rounded bg-gray-900 border border-gray-800 text-center text-xs text-gray-400">
                    No shared account groups configured. Register groups via POST /api/v1/admin/account-groups.
                  </div>
                ) : (
                  accountGroups.map((ag) => (
                    <div
                      key={ag.id}
                      className="p-3 rounded bg-gray-900 border border-gray-800 text-xs space-y-1"
                    >
                      <div className="font-semibold text-sm text-white">
                        {ag.name}
                      </div>
                      <div className="text-gray-400">
                        Provider:{" "}
                        <span className="font-mono text-gray-200">
                          {ag.provider_id}
                        </span>{" "}
                        • Shared RPM: {ag.shared_rpm_limit} • Shared TPM:{" "}
                        {formatNumber(ag.shared_tpm_limit)}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </section>
        )}

        {/* 4. MODELS & ALIASES */}
        {activeSection === "models" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              4. Model Registry & Semantic Model Aliases
            </h2>
            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <h3 className="text-base font-semibold mb-3">
                Semantic Routing Aliases (10 Built-in Aliases)
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {(aliases || []).map((a) => (
                  <div
                    key={a.alias}
                    className="p-3 rounded bg-gray-900 border border-gray-800 flex items-center justify-between"
                  >
                    <div>
                      <span className="font-mono text-sm font-bold text-blue-400">
                        model=&quot;{a.alias}&quot;
                      </span>
                      <p className="text-xs text-gray-400 mt-0.5">
                        {a.description}
                      </p>
                    </div>
                    <span className="text-xs px-2 py-1 rounded bg-gray-800 font-mono">
                      policy: {a.preferred_policy}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <h3 className="text-base font-semibold mb-3">
                Registered Models, Verified Pricing & EWMA Latency
              </h3>
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Canonical Model ID</th>
                    <th className="py-2">Context</th>
                    <th className="py-2">Quality / Rel.</th>
                    <th className="py-2">Input / Output ($/1M)</th>
                    <th className="py-2">EWMA p50 / TTFT</th>
                    <th className="py-2">Health</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {(models || []).map((m) => (
                    <tr key={m.model_id}>
                      <td className="py-2 font-mono font-medium">
                        {m.model_id}
                      </td>
                      <td className="py-2 text-xs">
                        {m.context_length
                          ? `${formatNumber(m.context_length)} tok`
                          : "UNKNOWN"}
                      </td>
                      <td className="py-2 text-xs">
                        Q: {m.quality_score} / R: {m.reliability_score}
                      </td>
                      <td className="py-2 text-xs font-mono">
                        {m.cost?.input_cost_per_1m_usd !== null &&
                        m.cost?.input_cost_per_1m_usd !== undefined
                          ? `$${m.cost.input_cost_per_1m_usd} / $${m.cost.output_cost_per_1m_usd}`
                          : "Unpriced / Audio"}
                      </td>
                      <td className="py-2 text-xs">
                        {m.latency?.p50_ms ?? "—"} ms /{" "}
                        {m.latency?.ttft_ms ?? "—"} ms
                      </td>
                      <td className="py-2">{renderBadge(m.health_status)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 5. ROUTING POLICIES */}
        {activeSection === "routing_policies" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              5. Routing Policy Engine & Explainability Simulator
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {(policies || []).map((p) => (
                <div
                  key={p.name}
                  className="p-4 rounded-lg bg-[#111827] border border-gray-800 space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono font-bold text-blue-400">
                      {p.name}
                    </span>
                    <span className="text-xs text-gray-400">
                      Max Fallbacks: {p.max_fallback_attempts}
                    </span>
                  </div>
                  <p className="text-xs text-gray-400">{p.description}</p>
                  <div className="grid grid-cols-5 gap-1 pt-2 text-center text-xs">
                    {Object.entries(p.weights || {}).map(([k, v]) => (
                      <div
                        key={k}
                        className="bg-gray-900 p-1.5 rounded border border-gray-800"
                      >
                        <div className="text-gray-400 capitalize">{k}</div>
                        <div className="font-semibold text-white">
                          {Math.round(v * 100)}%
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-3">
              <h3 className="text-base font-semibold">
                Interactive Route Decision Simulator
              </h3>
              <div className="flex flex-wrap gap-3">
                <input
                  type="text"
                  value={simAlias}
                  onChange={(e) => setSimAlias(e.target.value)}
                  placeholder="Alias or model (e.g. coding, fast, auto)"
                  className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                />
                <select
                  value={simPolicy}
                  onChange={(e) => setSimPolicy(e.target.value)}
                  className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                >
                  {(policies || []).map((p) => (
                    <option key={p.name} value={p.name}>
                      Policy: {p.name}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => void handleSimulateRoute()}
                  className="rounded bg-blue-600 hover:bg-blue-500 px-4 py-1.5 text-sm font-medium"
                >
                  Simulate Routing Decision
                </button>
              </div>
              {simTraceResult && (
                <pre className="p-3 rounded bg-gray-950 border border-gray-800 text-xs font-mono text-emerald-300 overflow-x-auto">
                  {simTraceResult}
                </pre>
              )}
            </div>
          </section>
        )}

        {/* 6. CLIENT API KEYS */}
        {activeSection === "client_keys" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              6. Client Gateway API Keys (SHA-256 Hashed)
            </h2>
            {canMutate && (
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800 space-y-3">
                <h3 className="text-sm font-semibold">
                  Issue New Client Gateway Key
                </h3>
                <div className="flex flex-wrap gap-3">
                  <input
                    type="text"
                    placeholder="Application / Team Name"
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                  />
                  <input
                    type="number"
                    placeholder="RPM Limit"
                    value={newKeyRpm}
                    onChange={(e) => setNewKeyRpm(Number(e.target.value))}
                    className="rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm w-36"
                  />
                  <button
                    onClick={() => void handleCreateClientKey()}
                    className="rounded bg-blue-600 hover:bg-blue-500 px-4 py-1.5 text-sm font-medium"
                  >
                    Generate Client Key
                  </button>
                </div>
                {/* Interactive Modal Popup for Newly Issued Key */}
                {newlyIssuedRawKey && (
                  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4 animate-in fade-in duration-200">
                    <div className="bg-[#111827] border border-gray-700 max-w-lg w-full p-6 rounded-xl shadow-2xl space-y-4">
                      <div className="flex items-center justify-between pb-2 border-b border-gray-800">
                        <div className="flex items-center gap-2 text-emerald-400 font-bold text-lg">
                          <Key className="w-5 h-5" />
                          <span>Client Gateway API Key Issued</span>
                        </div>
                        <button
                          onClick={() => {
                            setNewlyIssuedRawKey(null);
                            setCopiedKey(false);
                          }}
                          className="text-gray-400 hover:text-white p-1 rounded hover:bg-gray-800 transition"
                          title="Close dialog"
                        >
                          <X className="w-5 h-5" />
                        </button>
                      </div>

                      <div className="p-3.5 rounded-lg bg-amber-950/50 border border-amber-600/60 flex gap-3 text-amber-200 text-xs leading-relaxed">
                        <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
                        <div>
                          <span className="font-semibold block text-amber-300 mb-1">
                            Save this key now — It will never be shown again!
                          </span>
                          For security, the gateway only stores a one-way cryptographic SHA-256 hash. Once you close this pop-up, you will not be able to retrieve or view this secret key again.
                        </div>
                      </div>

                      <div className="space-y-1.5">
                        <label className="text-xs font-semibold uppercase text-gray-400">
                          Secret API Key
                        </label>
                        <div className="flex items-center gap-2 bg-gray-950 p-2.5 rounded-lg border border-gray-800">
                          <code className="flex-1 font-mono text-xs text-emerald-400 break-all select-all">
                            {newlyIssuedRawKey}
                          </code>
                          <button
                            onClick={() => void handleCopyRawKey()}
                            className="flex items-center gap-1.5 rounded bg-emerald-600 hover:bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-white shrink-0 transition"
                          >
                            {copiedKey ? (
                              <>
                                <Check className="w-3.5 h-3.5" />
                                Copied!
                              </>
                            ) : (
                              <>
                                <Copy className="w-3.5 h-3.5" />
                                Copy Key
                              </>
                            )}
                          </button>
                        </div>
                      </div>

                      <div className="pt-2 flex justify-end">
                        <button
                          onClick={() => {
                            setNewlyIssuedRawKey(null);
                            setCopiedKey(false);
                          }}
                          className="rounded bg-blue-600 hover:bg-blue-500 px-5 py-2 text-sm font-medium text-white transition"
                        >
                          I have saved my key (Close)
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Modal Dialog for Editing an Existing Key */}
            {editingClientKey && (
              <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4 animate-in fade-in duration-200">
                <div className="bg-[#111827] border border-gray-700 max-w-md w-full p-6 rounded-xl shadow-2xl space-y-4">
                  <div className="flex items-center justify-between pb-2 border-b border-gray-800">
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                      <Edit3 className="w-5 h-5 text-blue-400" />
                      Edit Client API Key
                    </h3>
                    <button
                      onClick={() => setEditingClientKey(null)}
                      className="text-gray-400 hover:text-white p-1 rounded hover:bg-gray-800 transition"
                    >
                      <X className="w-5 h-5" />
                    </button>
                  </div>

                  <div className="space-y-3 text-sm">
                    <div>
                      <label className="block text-xs font-medium text-gray-400 mb-1">
                        Key Name / Service
                      </label>
                      <input
                        type="text"
                        value={editKeyName}
                        onChange={(e) => setEditKeyName(e.target.value)}
                        className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                      />
                    </div>

                    <div>
                      <label className="block text-xs font-medium text-gray-400 mb-1">
                        Role & Permissions
                      </label>
                      <select
                        value={editKeyRole}
                        onChange={(e) => setEditKeyRole(e.target.value)}
                        className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                      >
                        <option value="CLIENT">CLIENT (Standard Data Plane)</option>
                        <option value="OPERATOR">OPERATOR (Ops & Management)</option>
                        <option value="READ_ONLY">READ_ONLY (Metrics & Audit)</option>
                        <option value="SUPER_ADMIN">SUPER_ADMIN (Full Control)</option>
                      </select>
                    </div>

                    <div>
                      <label className="block text-xs font-medium text-gray-400 mb-1">
                        Rate Limit (RPM)
                      </label>
                      <input
                        type="number"
                        value={editKeyRpm}
                        onChange={(e) => setEditKeyRpm(Number(e.target.value))}
                        className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                      />
                    </div>

                    <div>
                      <label className="block text-xs font-medium text-gray-400 mb-1">
                        Monthly Budget ($ USD, optional)
                      </label>
                      <input
                        type="number"
                        step="0.01"
                        placeholder="Leave blank for unlimited"
                        value={editKeyBudget}
                        onChange={(e) => setEditKeyBudget(e.target.value)}
                        className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-sm"
                      />
                    </div>

                    <div className="flex items-center gap-2 pt-1">
                      <input
                        type="checkbox"
                        id="editEnabledCheck"
                        checked={editKeyEnabled}
                        onChange={(e) => setEditKeyEnabled(e.target.checked)}
                        className="rounded border-gray-700 bg-gray-900 text-blue-600 focus:ring-0"
                      />
                      <label
                        htmlFor="editEnabledCheck"
                        className="text-xs font-medium text-gray-300 cursor-pointer"
                      >
                        Key is Enabled (Active)
                      </label>
                    </div>
                  </div>

                  <div className="pt-3 flex justify-end gap-2 border-t border-gray-800">
                    <button
                      onClick={() => setEditingClientKey(null)}
                      className="rounded bg-gray-800 hover:bg-gray-700 px-4 py-1.5 text-xs font-medium text-gray-300 transition"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => void handleSaveEditKey()}
                      className="rounded bg-blue-600 hover:bg-blue-500 px-4 py-1.5 text-xs font-medium text-white transition"
                    >
                      Save Changes
                    </button>
                  </div>
                </div>
              </div>
            )}

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Name</th>
                    <th className="py-2">Key Prefix</th>
                    <th className="py-2">Role</th>
                    <th className="py-2">RPM Limit</th>
                    <th className="py-2">Monthly Budget</th>
                    <th className="py-2">Status</th>
                    {canMutate && <th className="py-2 text-right">Actions</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {clientKeys.length === 0 ? (
                    <tr>
                      <td colSpan={canMutate ? 7 : 6} className="py-6 text-center text-xs text-gray-500">
                        No client API keys issued yet. Generate a client key above or via POST /api/v1/admin/client-keys.
                      </td>
                    </tr>
                  ) : (
                    clientKeys.map((k) => (
                      <tr key={k.id}>
                        <td className="py-2 font-medium">{k.name}</td>
                        <td className="py-2 font-mono text-xs text-gray-400">
                          {k.key_prefix}
                        </td>
                        <td className="py-2 font-mono text-xs">{k.role}</td>
                        <td className="py-2">{k.rate_limit_rpm} RPM</td>
                        <td className="py-2 text-xs text-gray-300">
                          {k.monthly_budget_usd !== null && k.monthly_budget_usd !== undefined
                            ? `$${k.monthly_budget_usd.toFixed(2)}`
                            : "Unlimited"}
                        </td>
                        <td className="py-2">
                          {renderBadge(k.enabled ? "ACTIVE" : "REVOKED")}
                        </td>
                        {canMutate && (
                          <td className="py-2 text-right">
                            <div className="flex items-center justify-end gap-2">
                              <button
                                onClick={() => handleOpenEditKey(k)}
                                className="p-1.5 rounded hover:bg-gray-800 text-blue-400 hover:text-blue-300 transition"
                                title="Edit client key"
                              >
                                <Edit3 className="w-4 h-4" />
                              </button>
                              <button
                                onClick={() => void handleToggleClientKey(k)}
                                className={`px-2 py-0.5 rounded text-xs font-medium transition ${
                                  k.enabled
                                    ? "bg-amber-950/60 border border-amber-700 text-amber-300 hover:bg-amber-900/60"
                                    : "bg-emerald-950/60 border border-emerald-700 text-emerald-300 hover:bg-emerald-900/60"
                                }`}
                                title={k.enabled ? "Revoke key" : "Enable key"}
                              >
                                {k.enabled ? "Revoke" : "Enable"}
                              </button>
                              <button
                                onClick={() => void handleDeleteClientKey(k)}
                                className="p-1.5 rounded hover:bg-gray-800 text-rose-400 hover:text-rose-300 transition"
                                title="Delete client key"
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
                            </div>
                          </td>
                        )}
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 7. USAGE & COST ANALYTICS */}
        {activeSection === "analytics" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              7. Usage, Token & Verified USD Cost Analytics
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">Input Tokens</div>
                <div className="text-xl font-bold mt-1">
                  {formatNumber(overview.total_input_tokens)}
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">Output Tokens</div>
                <div className="text-xl font-bold mt-1">
                  {formatNumber(overview.total_output_tokens)}
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#111827] border border-gray-800">
                <div className="text-xs text-gray-400">
                  Verified Estimated Spend (USD)
                </div>
                <div className="text-xl font-bold mt-1 text-emerald-400">
                  ${overview.total_estimated_cost_usd.toFixed(4)}
                </div>
              </div>
            </div>
          </section>
        )}

        {/* 8. REQUEST & TRACE LOGS */}
        {activeSection === "request_logs" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              8. Request & W3C Trace Telemetry Logs (Prompts Redacted)
            </h2>
            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Request ID</th>
                    <th className="py-2">Provider / Model</th>
                    <th className="py-2">Mode / Protocol</th>
                    <th className="py-2">Attempts / Fallback</th>
                    <th className="py-2">Tokens / Cost</th>
                    <th className="py-2">Latency</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {requestLogs.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="py-8 text-center text-xs text-gray-500">
                        No requests recorded yet. Send your first chat completion to <code className="text-blue-400">/v1/chat/completions</code> to view live request telemetry.
                      </td>
                    </tr>
                  ) : (
                    requestLogs.map((r) => (
                      <tr key={r.id}>
                        <td className="py-2 font-mono text-xs">
                          {r.request_id.slice(0, 12)}...
                        </td>
                        <td className="py-2 font-mono text-xs">
                          {r.provider_id}/{r.model_id}
                        </td>
                        <td className="py-2 text-xs">
                          {r.integration_mode} ({r.protocol})
                        </td>
                        <td className="py-2 text-xs">
                          {r.attempt_count} att •{" "}
                          {r.fallback_used ? "FALLBACK" : "PRIMARY"}
                        </td>
                        <td className="py-2 text-xs font-mono">
                          {r.total_tokens} tok (${r.estimated_cost_usd.toFixed(5)}
                          )
                        </td>
                        <td className="py-2 text-xs">{r.latency_ms} ms</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 9. TOOL REGISTRY */}
        {activeSection === "tools" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              9. Universal Tool Gateway Registry
            </h2>
            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800">
              <table className="w-full text-sm text-left">
                <thead className="text-xs uppercase text-gray-400 border-b border-gray-800">
                  <tr>
                    <th className="py-2">Tool Name</th>
                    <th className="py-2">Source</th>
                    <th className="py-2">Description</th>
                    <th className="py-2">Idempotent</th>
                    <th className="py-2">Timeout</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {(tools || []).map((t) => (
                    <tr key={t.name}>
                      <td className="py-2 font-mono font-medium">{t.name}</td>
                      <td className="py-2 font-mono text-xs">
                        {t.source_type}
                      </td>
                      <td className="py-2 text-xs text-gray-300">
                        {t.description}
                      </td>
                      <td className="py-2 text-xs">
                        {t.is_idempotent ? "Yes (Safe Retry)" : "No"}
                      </td>
                      <td className="py-2 text-xs">{t.timeout_seconds}s</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 10. MCP CONNECTIONS */}
        {activeSection === "mcp_servers" && (
          <section className="space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-2xl font-bold flex items-center gap-2">
                  <Plug className="w-6 h-6 text-blue-400" />
                  10. Model Context Protocol (MCP) Server Connections
                </h2>
                <p className="text-xs text-gray-400 mt-1">
                  Connect external MCP servers (JSON-RPC 2.0 / SSE) to dynamically discover and expose live tools to language models.
                </p>
              </div>
            </div>

            {canMutate && (
              <div className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2 text-white">
                  <PlusCircle className="w-4 h-4 text-emerald-400" />
                  Connect New MCP Server
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 text-sm">
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Server ID (Slug)
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. github-mcp"
                      value={newMcpId}
                      onChange={(e) => setNewMcpId(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs font-mono text-white"
                    />
                  </div>

                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Display Name
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. GitHub Dev Tools"
                      value={newMcpName}
                      onChange={(e) => setNewMcpName(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                    />
                  </div>

                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Transport Type
                    </label>
                    <select
                      value={newMcpTransport}
                      onChange={(e) => setNewMcpTransport(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                    >
                      <option value="http_jsonrpc">HTTP (JSON-RPC 2.0)</option>
                      <option value="sse">SSE (Server-Sent Events)</option>
                      <option value="in_memory">In-Memory</option>
                    </select>
                  </div>

                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Timeout (seconds)
                    </label>
                    <input
                      type="number"
                      value={newMcpTimeout}
                      onChange={(e) => setNewMcpTimeout(Number(e.target.value))}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                    />
                  </div>

                  <div className="md:col-span-2 lg:col-span-3">
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Endpoint URL
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. http://localhost:3001/mcp or https://mcp.internal.lan/sse"
                      value={newMcpUrl}
                      onChange={(e) => setNewMcpUrl(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs font-mono text-white"
                    />
                  </div>

                  <div className="flex items-end">
                    <button
                      onClick={() => void handleRegisterMcpServer()}
                      className="w-full rounded bg-blue-600 hover:bg-blue-500 py-1.5 px-4 text-xs font-medium text-white transition flex items-center justify-center gap-1.5"
                    >
                      <PlusCircle className="w-3.5 h-3.5" />
                      Connect Server
                    </button>
                  </div>
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 gap-4">
              {mcpServers.length === 0 ? (
                <div className="p-8 rounded-lg bg-[#111827] border border-gray-800 text-center text-xs text-gray-500">
                  No MCP servers currently connected. Use the form above or call POST /api/v1/mcp/servers.
                </div>
              ) : (
                mcpServers.map((s) => (
                  <div
                    key={s.server_id}
                    className="p-5 rounded-lg bg-[#111827] border border-gray-800 flex flex-wrap items-center justify-between gap-4"
                  >
                    <div>
                      <div className="font-semibold text-white flex items-center gap-2">
                        <Server className="w-4 h-4 text-blue-400" />
                        {s.name}
                      </div>
                      <div className="text-xs text-gray-400 font-mono mt-1">
                        ID: <span className="text-gray-200">{s.server_id}</span> • Transport:{" "}
                        <span className="text-emerald-400">{s.transport}</span> •{" "}
                        <span className="text-gray-300">{s.endpoint_url || "in-memory"}</span>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="px-2.5 py-1 rounded bg-gray-900 border border-gray-700 text-xs text-gray-300">
                        {s.discovered_tools_count} tool(s) synced
                      </span>
                      {renderBadge(s.health_status)}

                      {canMutate && (
                        <div className="flex items-center gap-2 ml-2">
                          <button
                            onClick={() => void handleSyncMcpTools(s.server_id)}
                            disabled={syncingMcpId === s.server_id}
                            className="flex items-center gap-1.5 px-3 py-1 rounded bg-emerald-950/70 border border-emerald-700 text-emerald-300 hover:bg-emerald-900/70 text-xs font-medium transition disabled:opacity-50"
                            title="Discover tools and sync into Gateway Tool Registry"
                          >
                            <RefreshCw
                              className={`w-3.5 h-3.5 ${
                                syncingMcpId === s.server_id ? "animate-spin" : ""
                              }`}
                            />
                            {syncingMcpId === s.server_id ? "Syncing..." : "Sync Tools"}
                          </button>
                          <button
                            onClick={() => void handleDeleteMcpServer(s.server_id)}
                            className="p-1.5 rounded hover:bg-gray-800 text-rose-400 hover:text-rose-300 transition"
                            title="Disconnect and delete MCP server"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        )}

        {/* 11. CUSTOM INTEGRATIONS */}
        {activeSection === "custom_integrations" && (
          <section className="space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-2xl font-bold flex items-center gap-2">
                  <Globe className="w-6 h-6 text-purple-400" />
                  11. Custom Provider & Protocol Plugins
                </h2>
                <p className="text-xs text-gray-400 mt-1">
                  Connect self-hosted inference servers (vLLM, Ollama, TGI, SGLang) or internal company AI microservices directly into the gateway.
                </p>
              </div>
            </div>

            {canMutate && (
              <div className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-4">
                <h3 className="text-sm font-semibold flex items-center gap-2 text-white">
                  <PlusCircle className="w-4 h-4 text-emerald-400" />
                  Register Custom REST AI Provider
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 text-sm">
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Provider Slug
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. vllm-cluster"
                      value={newCustomProvId}
                      onChange={(e) => setNewCustomProvId(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs font-mono text-white"
                    />
                  </div>

                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Display Name
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. On-Premises vLLM Service"
                      value={newCustomProvName}
                      onChange={(e) => setNewCustomProvName(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs text-white"
                    />
                  </div>

                  <div className="lg:col-span-1">
                    <label className="block text-xs font-medium text-gray-400 mb-1">
                      Base URL (OpenAI-compatible or REST)
                    </label>
                    <input
                      type="text"
                      placeholder="e.g. http://localhost:8000/v1"
                      value={newCustomProvBaseUrl}
                      onChange={(e) => setNewCustomProvBaseUrl(e.target.value)}
                      className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 text-xs font-mono text-white"
                    />
                  </div>

                  <div className="flex items-center gap-2 md:col-span-2 pt-2">
                    <input
                      type="checkbox"
                      id="allowLocalNetworkCheck"
                      checked={newCustomProvAllowLocal}
                      onChange={(e) => setNewCustomProvAllowLocal(e.target.checked)}
                      className="rounded border-gray-700 bg-gray-900 text-blue-600 focus:ring-0"
                    />
                    <label
                      htmlFor="allowLocalNetworkCheck"
                      className="text-xs text-gray-300 cursor-pointer"
                    >
                      Allow local/private network addresses (RFC1918 / localhost / Docker network)
                    </label>
                  </div>

                  <div className="flex items-end justify-end md:col-span-1 pt-2">
                    <button
                      onClick={() => void handleRegisterCustomProvider()}
                      className="w-full rounded bg-blue-600 hover:bg-blue-500 py-1.5 px-4 text-xs font-medium text-white transition flex items-center justify-center gap-1.5"
                    >
                      <PlusCircle className="w-3.5 h-3.5" />
                      Register Provider
                    </button>
                  </div>
                </div>
              </div>
            )}

            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-4">
              <h3 className="text-base font-semibold text-white">
                Active Custom Provider Adapters
              </h3>
              <div className="space-y-3">
                {providers.filter((p) => p.adapter_type === "custom_http" || !["openai", "anthropic", "gemini", "groq", "ollama", "openrouter", "elevenlabs"].includes(p.provider_id)).length === 0 ? (
                  <p className="text-xs text-gray-500 py-4 text-center">
                    No custom AI providers registered. Register one above to connect your local or private cluster.
                  </p>
                ) : (
                  providers
                    .filter((p) => p.adapter_type === "custom_http" || !["openai", "anthropic", "gemini", "groq", "ollama", "openrouter", "elevenlabs"].includes(p.provider_id))
                    .map((p) => (
                      <div
                        key={p.provider_id}
                        className="p-4 rounded-lg bg-gray-950 border border-gray-800 flex flex-wrap items-center justify-between gap-4"
                      >
                        <div>
                          <div className="font-semibold text-white flex items-center gap-2">
                            <Globe className="w-4 h-4 text-purple-400" />
                            {p.display_name}
                          </div>
                          <div className="text-xs text-gray-400 font-mono mt-1">
                            Slug: <span className="text-gray-200">{p.provider_id}</span> • Base URL:{" "}
                            <span className="text-blue-400">{p.base_url}</span>
                          </div>
                          <div className="text-xs text-gray-500 font-mono mt-1">
                            Endpoints: /v1/custom/{p.provider_id} & /v1/native/{p.provider_id}/...
                          </div>
                        </div>

                        <div className="flex items-center gap-3">
                          {renderBadge(p.health_status || "HEALTHY")}
                          {canMutate && (
                            <button
                              onClick={() => void handleDeleteCustomProvider(p.provider_id)}
                              className="p-1.5 rounded hover:bg-gray-800 text-rose-400 hover:text-rose-300 transition"
                              title="Unregister custom provider"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          )}
                        </div>
                      </div>
                    ))
                )}
              </div>
            </div>

            <div className="p-4 rounded-lg bg-gray-900/50 border border-gray-800 text-xs text-gray-300 space-y-2">
              <span className="font-semibold text-white block">How to query custom providers:</span>
              <ul className="list-disc list-inside space-y-1 text-gray-400 font-mono">
                <li>
                  Unified Custom Protocol: <span className="text-blue-300">POST /v1/custom/&#123;provider_id&#125;</span>
                </li>
                <li>
                  Lossless Native Passthrough: <span className="text-blue-300">POST /v1/native/&#123;provider_id&#125;/chat/completions</span>
                </li>
              </ul>
            </div>
          </section>
        )}

        {/* 12. SYSTEM SETTINGS */}
        {activeSection === "system_settings" && (
          <section className="space-y-6">
            <h2 className="text-2xl font-bold">
              12. System Settings & Security Boundary Configuration
            </h2>
            <div className="p-5 rounded-lg bg-[#111827] border border-gray-800 space-y-4 text-sm">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs text-gray-400">
                    Gateway Backend URL
                  </label>
                  <input
                    type="text"
                    value={gatewayUrl}
                    onChange={(e) => setGatewayUrl(e.target.value)}
                    className="mt-1 w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400">
                    Admin Bootstrap Token (In-Memory Only)
                  </label>
                  <input
                    type="password"
                    value={adminToken}
                    onChange={(e) => setAdminToken(e.target.value)}
                    className="mt-1 w-full rounded bg-gray-900 border border-gray-700 px-3 py-1.5 font-mono text-xs"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2 text-xs">
                <div className="p-3 rounded bg-gray-900 border border-gray-800">
                  <div className="text-gray-400">Encryption</div>
                  <div className="font-mono font-semibold mt-1">
                    {systemSettings?.encryption_algorithm || "AES-256-GCM"}
                  </div>
                </div>
                <div className="p-3 rounded bg-gray-900 border border-gray-800">
                  <div className="text-gray-400">Database Dialect</div>
                  <div className="font-mono font-semibold mt-1">
                    {systemSettings?.database_dialect || "PostgreSQL / SQLite"}
                  </div>
                </div>
                <div className="p-3 rounded bg-gray-900 border border-gray-800">
                  <div className="text-gray-400">Redis Backend</div>
                  <div className="font-mono font-semibold mt-1">
                    {systemSettings?.redis_backend || "Coordinated (or In-Memory)"}
                  </div>
                </div>
                <div className="p-3 rounded bg-gray-900 border border-gray-800">
                  <div className="text-gray-400">SSRF & Secret Redaction</div>
                  <div className="font-mono font-semibold text-emerald-400 mt-1">
                    ENFORCED
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}

