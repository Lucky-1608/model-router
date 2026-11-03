"""Prometheus-compatible Metrics Collector for the Universal AI Gateway."""

from __future__ import annotations

from collections import defaultdict
from gateway.health_monitor.models import CircuitBreakerSnapshot, CircuitBreakerState


def _escape_label(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


class MetricsCollector:
    """In-memory Prometheus-compatible metrics collector with cardinality guard and text exposition."""

    def __init__(self, *, max_distinct_series: int = 2000) -> None:
        self._max_distinct_series = max_distinct_series
        self._requests_total: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._latency_sum_ms: dict[tuple[str, str], float] = defaultdict(float)
        self._latency_count: dict[tuple[str, str], int] = defaultdict(int)
        self._ttft_sum_ms: dict[tuple[str, str], float] = defaultdict(float)
        self._ttft_count: dict[tuple[str, str], int] = defaultdict(int)
        self._tokens_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._cost_usd_total: dict[tuple[str, str], float] = defaultdict(float)
        self._fallback_total: dict[tuple[str, str], int] = defaultdict(int)

    def _normalize_model_id(self, provider_id: str, model_id: str) -> str:
        # If cardinality threshold reached for this provider/model pair, collapse arbitrary strings to "_other"
        key = (provider_id, model_id)
        if key not in self._latency_count and len(self._latency_count) >= self._max_distinct_series:
            return "_other"
        return model_id

    def record_request(
        self,
        *,
        provider_id: str,
        model_id: str,
        protocol: str,
        status: str,
        latency_ms: float,
        ttft_ms: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        fallback_used: bool = False,
        primary_provider_id: str | None = None,
    ) -> None:
        """Records metrics for a completed gateway request with cardinality protection."""
        norm_model = self._normalize_model_id(provider_id, model_id)
        self._requests_total[(provider_id, norm_model, protocol, status)] += 1
        self._latency_sum_ms[(provider_id, norm_model)] += latency_ms
        self._latency_count[(provider_id, norm_model)] += 1

        if ttft_ms is not None:
            self._ttft_sum_ms[(provider_id, norm_model)] += ttft_ms
            self._ttft_count[(provider_id, norm_model)] += 1

        if input_tokens > 0:
            self._tokens_total[(provider_id, norm_model, "input")] += input_tokens
        if output_tokens > 0:
            self._tokens_total[(provider_id, norm_model, "output")] += output_tokens

        if cost_usd > 0.0:
            self._cost_usd_total[(provider_id, norm_model)] += cost_usd

        if fallback_used:
            from_prov = primary_provider_id or provider_id
            self._fallback_total[(from_prov, provider_id)] += 1

    def format_prometheus_text(
        self,
        circuit_snapshots: list[CircuitBreakerSnapshot] | None = None,
    ) -> str:
        """Renders all collected metrics in standard Prometheus text format (0.0.4)."""
        lines: list[str] = []

        # 1. ugw_requests_total
        lines.append("# HELP ugw_requests_total Total number of AI Gateway requests.")
        lines.append("# TYPE ugw_requests_total counter")
        for (prov, mod, proto, status), count in sorted(self._requests_total.items()):
            lines.append(
                f'ugw_requests_total{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}",protocol="{_escape_label(proto)}",status="{_escape_label(status)}"}} {count}'
            )

        # 2. ugw_request_latency_ms
        lines.append("# HELP ugw_request_latency_ms_sum Total request latency in milliseconds.")
        lines.append("# TYPE ugw_request_latency_ms_sum counter")
        lines.append("# HELP ugw_request_latency_ms_count Count of latency measurements.")
        lines.append("# TYPE ugw_request_latency_ms_count counter")
        for (prov, mod), total_lat in sorted(self._latency_sum_ms.items()):
            cnt = self._latency_count[(prov, mod)]
            lines.append(
                f'ugw_request_latency_ms_sum{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}"}} {round(total_lat, 2)}'
            )
            lines.append(
                f'ugw_request_latency_ms_count{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}"}} {cnt}'
            )

        # 3. ugw_ttft_ms
        lines.append("# HELP ugw_ttft_ms_sum Total time to first token in milliseconds.")
        lines.append("# TYPE ugw_ttft_ms_sum counter")
        lines.append("# HELP ugw_ttft_ms_count Count of TTFT measurements.")
        lines.append("# TYPE ugw_ttft_ms_count counter")
        for (prov, mod), total_ttft in sorted(self._ttft_sum_ms.items()):
            cnt = self._ttft_count[(prov, mod)]
            lines.append(
                f'ugw_ttft_ms_sum{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}"}} {round(total_ttft, 2)}'
            )
            lines.append(
                f'ugw_ttft_ms_count{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}"}} {cnt}'
            )

        # 4. ugw_tokens_total
        lines.append("# HELP ugw_tokens_total Total tokens processed by direction.")
        lines.append("# TYPE ugw_tokens_total counter")
        for (prov, mod, direction), tok_cnt in sorted(self._tokens_total.items()):
            lines.append(
                f'ugw_tokens_total{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}",direction="{_escape_label(direction)}"}} {tok_cnt}'
            )

        # 5. ugw_estimated_cost_usd_total
        lines.append("# HELP ugw_estimated_cost_usd_total Total estimated cost in USD.")
        lines.append("# TYPE ugw_estimated_cost_usd_total counter")
        for (prov, mod), cost_val in sorted(self._cost_usd_total.items()):
            lines.append(
                f'ugw_estimated_cost_usd_total{{provider="{_escape_label(prov)}",model="{_escape_label(mod)}"}} {round(cost_val, 8)}'
            )

        # 6. ugw_fallback_executions_total
        lines.append("# HELP ugw_fallback_executions_total Total fallback executions triggered.")
        lines.append("# TYPE ugw_fallback_executions_total counter")
        for (from_p, to_p), fb_cnt in sorted(self._fallback_total.items()):
            lines.append(
                f'ugw_fallback_executions_total{{from_provider="{_escape_label(from_p)}",to_provider="{_escape_label(to_p)}"}} {fb_cnt}'
            )

        # 7. ugw_circuit_breaker_state (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
        lines.append(
            "# HELP ugw_circuit_breaker_state Circuit breaker state (0=CLOSED, 1=HALF_OPEN, 2=OPEN)."
        )
        lines.append("# TYPE ugw_circuit_breaker_state gauge")
        if circuit_snapshots:
            state_map = {
                CircuitBreakerState.CLOSED: 0,
                CircuitBreakerState.HALF_OPEN: 1,
                CircuitBreakerState.OPEN: 2,
            }
            for snap in circuit_snapshots:
                val = state_map.get(snap.state, 0)
                lines.append(
                    f'ugw_circuit_breaker_state{{scope="{_escape_label(snap.scope.value)}",target="{_escape_label(snap.target_id)}"}} {val}'
                )

        return "\n".join(lines) + "\n"

