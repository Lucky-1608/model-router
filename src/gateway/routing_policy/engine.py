"""Routing Policy Engine managing built-in and custom routing policies."""

from __future__ import annotations

from gateway.config.settings import GatewaySettings
from gateway.core.exceptions import ResourceNotFoundError
from gateway.routing_policy.models import RoutingPolicySpec, RoutingWeights


class RoutingPolicyEngine:
    """Manages routing policies and their configurable scoring weights."""

    def __init__(self, settings: GatewaySettings | None = None) -> None:
        self._policies: dict[str, RoutingPolicySpec] = {}
        self._seed_default_policies(settings)

    def _seed_default_policies(
        self, settings: GatewaySettings | None = None
    ) -> None:
        balanced_weights = (
            RoutingWeights(
                quality=settings.default_weight_quality,
                reliability=settings.default_weight_reliability,
                cost=settings.default_weight_cost,
                latency=settings.default_weight_latency,
                capacity=settings.default_weight_capacity,
            )
            if settings
            else RoutingWeights()
        )
        defaults = [
            RoutingPolicySpec(
                name="balanced",
                description="Default balanced policy (Quality 30%, Reliability 25%, Cost 20%, Latency 20%, Capacity 5%)",
                weights=balanced_weights,
            ),
            RoutingPolicySpec(
                name="quality_first",
                description="Prioritizes model accuracy and task suitability (Quality 75%, Reliability 15%, Cost 2%, Latency 3%, Capacity 5%)",
                weights=RoutingWeights(
                    quality=0.75,
                    reliability=0.15,
                    cost=0.02,
                    latency=0.03,
                    capacity=0.05,
                ),
            ),
            RoutingPolicySpec(
                name="cost_optimized",
                description="Minimizes token/character cost while maintaining reliability (Cost 55%, Quality 15%, Reliability 15%, Latency 10%, Capacity 5%)",
                weights=RoutingWeights(
                    quality=0.15,
                    reliability=0.15,
                    cost=0.55,
                    latency=0.10,
                    capacity=0.05,
                ),
            ),
            RoutingPolicySpec(
                name="latency_optimized",
                description="Minimizes P50/TTFT latency for real-time responsiveness (Latency 55%, Quality 15%, Reliability 15%, Cost 10%, Capacity 5%)",
                weights=RoutingWeights(
                    quality=0.15,
                    reliability=0.15,
                    cost=0.10,
                    latency=0.55,
                    capacity=0.05,
                ),
            ),
            RoutingPolicySpec(
                name="reliability_first",
                description="Maximizes uptime, circuit health, and available capacity (Reliability 55%, Quality 20%, Cost 10%, Latency 5%, Capacity 10%)",
                weights=RoutingWeights(
                    quality=0.20,
                    reliability=0.55,
                    cost=0.10,
                    latency=0.05,
                    capacity=0.10,
                ),
            ),
        ]
        for pol in defaults:
            self._policies[pol.name] = pol

    def get_policy(self, name: str | None = None) -> RoutingPolicySpec:
        key = (name or "balanced").strip().lower()
        pol = self._policies.get(key)
        if pol is None:
            raise ResourceNotFoundError(
                f"Routing policy {name!r} is not defined", param="policy"
            )
        return pol

    def list_policies(self) -> list[RoutingPolicySpec]:
        return sorted(self._policies.values(), key=lambda p: p.name)

    def upsert_policy(self, spec: RoutingPolicySpec) -> RoutingPolicySpec:
        key = spec.name.strip().lower()
        normalized = spec.model_copy(update={"name": key})
        self._policies[key] = normalized
        return normalized
