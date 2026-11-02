"""Capability Registry module — explicit SUPPORTED/UNSUPPORTED/UNKNOWN capability manifests."""

from gateway.capability_registry.models import (
    CapabilityFlag,
    CapabilityState,
    ProtocolType,
    ProviderCapabilityManifest,
    QuotaScopeType,
    build_capability_map,
)

__all__ = [
    "CapabilityFlag",
    "CapabilityState",
    "ProtocolType",
    "ProviderCapabilityManifest",
    "QuotaScopeType",
    "build_capability_map",
]
