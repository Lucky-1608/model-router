"""Credential Manager module — encrypted multi-key storage, health states, and rotation strategies."""

from gateway.credential_manager.crypto import (
    EncryptedEnvelope,
    EnvelopeEncryptor,
    compute_key_fingerprint,
)
from gateway.credential_manager.manager import CredentialLease, CredentialManager
from gateway.credential_manager.models import (
    AccountGroupCreateRequest,
    AccountGroupPublicView,
    AccountGroupRecord,
    CredentialAuditEntryView,
    CredentialAuditLogRecord,
    CredentialCreateRequest,
    CredentialHealthView,
    CredentialPublicView,
    CredentialRecord,
    CredentialStatus,
    CredentialUpdateRequest,
    CredentialUsageRecord,
    QuotaConfigSpec,
    SelectionStrategy,
)
from gateway.credential_manager.retry_policy import RetryPolicyConfig
from gateway.credential_manager.router import credential_admin_router

__all__ = [
    "AccountGroupCreateRequest",
    "AccountGroupPublicView",
    "AccountGroupRecord",
    "CredentialAuditEntryView",
    "CredentialAuditLogRecord",
    "CredentialCreateRequest",
    "CredentialHealthView",
    "CredentialLease",
    "CredentialManager",
    "CredentialPublicView",
    "CredentialRecord",
    "CredentialStatus",
    "CredentialUpdateRequest",
    "CredentialUsageRecord",
    "EncryptedEnvelope",
    "EnvelopeEncryptor",
    "QuotaConfigSpec",
    "RetryPolicyConfig",
    "SelectionStrategy",
    "compute_key_fingerprint",
    "credential_admin_router",
]
