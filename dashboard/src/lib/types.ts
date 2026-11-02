/**
 * TypeScript domain models for the Universal AI Gateway Management Dashboard.
 * Never includes unmasked provider secrets or decrypted key material.
 */

export type DataSourceMode = "LIVE_BACKEND" | "DEMO_FALLBACK";

export type AdminRole = "SUPER_ADMIN" | "OPERATOR" | "READ_ONLY";

export type DashboardSectionId =
  | "overview"
  | "providers"
  | "credentials"
  | "models"
  | "routing_policies"
  | "client_keys"
  | "analytics"
  | "request_logs"
  | "tools"
  | "mcp_servers"
  | "custom_integrations"
  | "system_settings";

export interface AnalyticsOverview {
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  success_rate: number;
  fallback_requests: number;
  fallback_rate: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_characters: number;
  total_estimated_cost_usd: number;
  avg_latency_ms: number;
  avg_ttft_ms: number | null;
}

export interface ProviderSummary {
  provider_id: string;
  display_name: string;
  enabled: boolean;
  base_url: string;
  supported_protocols: string[];
  capabilities: Record<string, "SUPPORTED" | "UNSUPPORTED" | "UNKNOWN">;
  health_status?: "HEALTHY" | "DEGRADED" | "UNHEALTHY" | "UNKNOWN";
}

export interface CredentialPublicView {
  id: string;
  provider_id: string;
  label: string;
  masked_hint: string;
  status:
    | "ACTIVE"
    | "DISABLED"
    | "INVALID"
    | "COOLING_DOWN"
    | "TEMPORARILY_UNAVAILABLE"
    | "REQUIRES_ADMIN_ATTENTION"
    | "REVOKED";
  weight: number;
  priority: number;
  account_group_id: string | null;
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_concurrency: number;
  health_score: number;
}

export interface AccountGroupView {
  id: string;
  provider_id: string;
  name: string;
  shared_rpm_limit: number | null;
  shared_tpm_limit: number | null;
  shared_rpd_limit: number | null;
  shared_max_concurrency: number;
}

export interface ModelMetadataView {
  model_id: string;
  provider_id: string;
  provider_model_id: string;
  display_name: string;
  enabled: boolean;
  context_length: number | null;
  max_output_tokens: number | null;
  capabilities: Record<string, "SUPPORTED" | "UNSUPPORTED" | "UNKNOWN">;
  quality_score: number;
  reliability_score: number;
  health_status: "HEALTHY" | "DEGRADED" | "UNHEALTHY" | "UNKNOWN";
  cost: {
    input_cost_per_1m_usd: number | null;
    output_cost_per_1m_usd: number | null;
    per_character_usd: number | null;
  };
  latency: {
    p50_ms: number | null;
    p95_ms: number | null;
    ttft_ms: number | null;
  };
}

export interface ModelAliasView {
  alias: string;
  description: string;
  default_task: string;
  preferred_policy: string;
  required_capabilities: string[];
}

export interface RoutingPolicyView {
  name: string;
  description: string;
  weights: {
    quality: number;
    reliability: number;
    cost: number;
    latency: number;
    capacity: number;
  };
  allow_cross_provider_fallback: boolean;
  max_fallback_attempts: number;
}

export interface ClientApiKeyView {
  id: string;
  name: string;
  key_prefix: string;
  role: string;
  allowed_providers: string[];
  allowed_tools: string[];
  rate_limit_rpm: number;
  monthly_budget_usd: number | null;
  enabled: boolean;
  created_at: string;
  api_key?: string;
}

export interface UsageRecordView {
  id: string;
  request_id: string;
  decision_id: string | null;
  provider_id: string;
  model_id: string;
  credential_id: string | null;
  protocol: string;
  integration_mode: string;
  stream: boolean;
  status_code: number;
  success: boolean;
  error_code: string | null;
  attempt_count: number;
  fallback_used: boolean;
  tool_rounds: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  latency_ms: number;
  ttft_ms: number | null;
  created_at: string;
}

export interface ToolSpecView {
  name: string;
  description: string;
  source_type: "builtin" | "webhook" | "mcp" | "provider_native";
  enabled: boolean;
  is_idempotent: boolean;
  timeout_seconds: number;
  mcp_server_id: string | null;
}

export interface MCPServerView {
  server_id: string;
  name: string;
  transport: "http" | "sse" | "memory";
  endpoint_url: string | null;
  enabled: boolean;
  health_status: string;
  discovered_tools_count: number;
  allowed_tools: string[];
  blocked_tools: string[];
}

export interface CircuitBreakerView {
  scope: "provider" | "model" | "credential";
  target_id: string;
  state: "CLOSED" | "OPEN" | "HALF_OPEN";
  failure_count: number;
  remaining_open_seconds: number;
  last_error_code: string | null;
  health_status: string;
}

export interface SystemSettingsView {
  app_name: string;
  version: string;
  environment: string;
  api_v1_prefix: string;
  database_dialect: string;
  redis_backend: string;
  default_routing_policy: string;
  default_routing_weights: Record<string, number>;
  http_timeout_connect_seconds: number;
  http_timeout_read_seconds: number;
  http_timeout_write_seconds: number;
  http_timeout_pool_seconds: number;
  encryption_algorithm: string;
  secret_redaction_enabled: boolean;
  ssrf_protection_enabled: boolean;
  extra_metadata: Record<string, string>;
}

