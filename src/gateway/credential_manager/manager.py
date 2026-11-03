"""Multi-key Credential Manager with encrypted storage, rotation algorithms, and retry coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar
import uuid

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from gateway.capability_registry.models import QuotaScopeType
from gateway.config.settings import GatewaySettings
from gateway.core.canonical import (
    AdapterCredential,
    HealthCheckResult,
    HealthStatus,
    RateLimitSnapshot,
    TokenUsage,
)
from gateway.core.db import DatabaseManager
from gateway.core.exceptions import (
    AuthenticationError,
    RateLimitExceededError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UpstreamProviderError,
)
from gateway.core.logging import get_logger
from gateway.core.redis import RedisManager
from gateway.core.security import redact_sensitive_data
from gateway.credential_manager.crypto import EnvelopeEncryptor
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
from gateway.provider_registry.registry import ProviderRegistry
from gateway.quota_manager.manager import QuotaEligibilityResult, QuotaManager

logger = get_logger("gateway.credential_manager")

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CredentialLease:
    """Represents an active concurrency and quota reservation on a selected credential."""

    request_id: str
    credential: AdapterCredential
    account_group_id: str | None
    workspace_id: str | None
    label: str
    masked_preview: str


class CredentialManager:
    """Independent subsystem managing encrypted multi-provider credentials, quotas, and rotation."""

    def __init__(
        self,
        settings: GatewaySettings,
        db_manager: DatabaseManager,
        redis_manager: RedisManager,
        provider_registry: ProviderRegistry,
        retry_policy: RetryPolicyConfig | None = None,
    ) -> None:
        self._settings = settings
        self._db = db_manager
        self._redis_manager = redis_manager
        self._provider_registry = provider_registry
        self._quota_manager = QuotaManager(redis_manager)
        self._encryptor = EnvelopeEncryptor(settings.get_master_kek_bytes())
        self._retry_policy = retry_policy or RetryPolicyConfig()
        self._selection_lock = asyncio.Lock()

    @property
    def quota_manager(self) -> QuotaManager:
        return self._quota_manager

    async def ensure_tables(self) -> None:
        """Ensures credential management tables exist in the database."""
        async with self._db.engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: AccountGroupRecord.metadata.create_all(
                    sync_conn,
                    tables=[
                        AccountGroupRecord.__table__,
                        CredentialRecord.__table__,
                        CredentialUsageRecord.__table__,
                        CredentialAuditLogRecord.__table__,
                    ],
                )
            )

    def _to_public_view(self, record: CredentialRecord) -> CredentialPublicView:
        return CredentialPublicView(
            id=str(record.id),
            provider_id=record.provider_id,
            account_group_id=str(record.account_group_id)
            if record.account_group_id
            else None,
            workspace_id=record.workspace_id,
            label=record.label,
            key_fingerprint=record.key_fingerprint,
            masked_preview=record.masked_preview,
            key_version=record.key_version,
            status=CredentialStatus(record.status),
            weight=record.weight,
            max_concurrency=record.max_concurrency,
            quota_config=QuotaConfigSpec(**(record.quota_config or {})),
            allowed_models=record.allowed_models,
            organization_id=record.organization_id,
            project_id=record.project_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def _write_audit_log(
        self,
        session: Any,
        *,
        actor_id: str,
        action: str,
        credential_id: str,
        provider_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_details = redact_sensitive_data(details or {})
        entry = CredentialAuditLogRecord(
            actor_id=actor_id,
            action=action,
            credential_id=credential_id,
            provider_id=provider_id,
            details_json=safe_details,
        )
        session.add(entry)

    # =========================================================================
    # Account Groups (Shared Organizational / Workspace Quotas)
    # =========================================================================
    async def create_account_group(
        self,
        req: AccountGroupCreateRequest,
        *,
        actor_id: str = "admin",
    ) -> AccountGroupPublicView:
        self._provider_registry.get_adapter(req.provider_id)
        async with self._db.session_factory() as session:
            rec = AccountGroupRecord(
                provider_id=req.provider_id,
                name=req.name,
                workspace_id=req.workspace_id,
                external_org_id=req.external_org_id,
                max_concurrency=req.max_concurrency,
                quota_config=req.quota_config.model_dump(),
            )
            session.add(rec)
            await session.commit()
            await session.refresh(rec)
            return AccountGroupPublicView(
                id=str(rec.id),
                provider_id=rec.provider_id,
                name=rec.name,
                workspace_id=rec.workspace_id,
                external_org_id=rec.external_org_id,
                max_concurrency=rec.max_concurrency,
                quota_config=QuotaConfigSpec(**rec.quota_config),
                created_at=rec.created_at,
            )

    async def list_account_groups(
        self, provider_id: str | None = None
    ) -> list[AccountGroupPublicView]:
        async with self._db.session_factory() as session:
            stmt = select(AccountGroupRecord)
            if provider_id:
                stmt = stmt.where(AccountGroupRecord.provider_id == provider_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                AccountGroupPublicView(
                    id=str(r.id),
                    provider_id=r.provider_id,
                    name=r.name,
                    workspace_id=r.workspace_id,
                    external_org_id=r.external_org_id,
                    max_concurrency=r.max_concurrency,
                    quota_config=QuotaConfigSpec(**(r.quota_config or {})),
                    created_at=r.created_at,
                )
                for r in rows
            ]

    # =========================================================================
    # Credential Lifecycle Operations
    # =========================================================================
    async def create_credential(
        self,
        req: CredentialCreateRequest,
        *,
        actor_id: str = "admin",
    ) -> CredentialPublicView:
        self._provider_registry.get_adapter(req.provider_id)
        cred_uuid = uuid.uuid4()
        envelope = self._encryptor.encrypt(
            req.secret.get_secret_value(),
            provider_id=req.provider_id,
            credential_id=str(cred_uuid),
        )
        acct_uuid = (
            uuid.UUID(req.account_group_id) if req.account_group_id else None
        )

        async with self._db.session_factory() as session:
            record = CredentialRecord(
                id=cred_uuid,
                provider_id=req.provider_id,
                account_group_id=acct_uuid,
                workspace_id=req.workspace_id,
                label=req.label,
                key_fingerprint=envelope.key_fingerprint,
                masked_preview=envelope.masked_preview,
                encrypted_secret=envelope.encrypted_secret,
                encrypted_dek=envelope.encrypted_dek,
                key_version=envelope.key_version,
                status=CredentialStatus.ACTIVE.value,
                weight=req.weight,
                max_concurrency=req.max_concurrency,
                quota_config=req.quota_config.model_dump(),
                allowed_models=req.allowed_models,
                organization_id=req.organization_id,
                project_id=req.project_id,
                extra_headers=req.extra_headers,
            )
            session.add(record)
            await self._write_audit_log(
                session,
                actor_id=actor_id,
                action="CREATE_CREDENTIAL",
                credential_id=str(cred_uuid),
                provider_id=req.provider_id,
                details={
                    "label": req.label,
                    "fingerprint": envelope.key_fingerprint,
                    "account_group_id": req.account_group_id,
                },
            )
            await session.commit()
            await session.refresh(record)
            logger.info(
                "credential_created",
                credential_id=str(cred_uuid),
                provider_id=req.provider_id,
                fingerprint=envelope.key_fingerprint,
            )
            return self._to_public_view(record)

    async def get_credential(self, credential_id: str) -> CredentialPublicView:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            return self._to_public_view(record)

    async def list_credentials(
        self, provider_id: str | None = None
    ) -> list[CredentialPublicView]:
        async with self._db.session_factory() as session:
            stmt = select(CredentialRecord)
            if provider_id:
                stmt = stmt.where(CredentialRecord.provider_id == provider_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_public_view(r) for r in rows]

    async def update_credential(
        self,
        credential_id: str,
        req: CredentialUpdateRequest,
        *,
        actor_id: str = "admin",
    ) -> CredentialPublicView:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            changed_fields: list[str] = []

            if req.label is not None:
                record.label = req.label
                changed_fields.append("label")
            if req.secret is not None:
                envelope = self._encryptor.encrypt(
                    req.secret.get_secret_value(),
                    provider_id=record.provider_id,
                    credential_id=str(record.id),
                )
                record.encrypted_secret = envelope.encrypted_secret
                record.encrypted_dek = envelope.encrypted_dek
                record.key_fingerprint = envelope.key_fingerprint
                record.masked_preview = envelope.masked_preview
                changed_fields.append("secret_rotated")
            if req.account_group_id is not None:
                record.account_group_id = (
                    uuid.UUID(req.account_group_id) if req.account_group_id else None
                )
                changed_fields.append("account_group_id")
            if req.workspace_id is not None:
                record.workspace_id = req.workspace_id
                changed_fields.append("workspace_id")
            if req.weight is not None:
                record.weight = req.weight
                changed_fields.append("weight")
            if req.max_concurrency is not None:
                record.max_concurrency = req.max_concurrency
                changed_fields.append("max_concurrency")
            if req.quota_config is not None:
                record.quota_config = req.quota_config.model_dump()
                changed_fields.append("quota_config")
            if req.allowed_models is not None:
                record.allowed_models = req.allowed_models
                changed_fields.append("allowed_models")
            if req.organization_id is not None:
                record.organization_id = req.organization_id
                changed_fields.append("organization_id")
            if req.project_id is not None:
                record.project_id = req.project_id
                changed_fields.append("project_id")
            if req.extra_headers is not None:
                record.extra_headers = req.extra_headers
                changed_fields.append("extra_headers")

            record.updated_at = datetime.now(timezone.utc)
            await self._write_audit_log(
                session,
                actor_id=actor_id,
                action="UPDATE_CREDENTIAL",
                credential_id=str(record.id),
                provider_id=record.provider_id,
                details={"changed_fields": changed_fields},
            )
            await session.commit()
            await session.refresh(record)
            return self._to_public_view(record)

    async def _transition_status(
        self,
        credential_id: str,
        new_status: CredentialStatus,
        *,
        actor_id: str = "system",
        action: str,
        reason: str | None = None,
    ) -> CredentialPublicView:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            prev_status = record.status
            record.status = new_status.value
            record.updated_at = datetime.now(timezone.utc)
            await self._write_audit_log(
                session,
                actor_id=actor_id,
                action=action,
                credential_id=str(record.id),
                provider_id=record.provider_id,
                details={
                    "previous_status": prev_status,
                    "new_status": new_status.value,
                    "reason": reason,
                },
            )
            await session.commit()
            await session.refresh(record)
            return self._to_public_view(record)

    async def enable_credential(
        self, credential_id: str, *, actor_id: str = "admin"
    ) -> CredentialPublicView:
        await self._redis_manager.client.delete(f"ugw:cd:cred:{credential_id}")
        await self._redis_manager.client.hset(
            f"ugw:cred:{credential_id}:state", mapping={"consecutive_failures": "0"}
        )
        return await self._transition_status(
            credential_id,
            CredentialStatus.ACTIVE,
            actor_id=actor_id,
            action="ENABLE_CREDENTIAL",
        )

    async def disable_credential(
        self, credential_id: str, *, actor_id: str = "admin"
    ) -> CredentialPublicView:
        return await self._transition_status(
            credential_id,
            CredentialStatus.DISABLED,
            actor_id=actor_id,
            action="DISABLE_CREDENTIAL",
        )

    async def revoke_credential(
        self, credential_id: str, *, actor_id: str = "admin"
    ) -> CredentialPublicView:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            # Zero out secret material on revocation while preserving audit trail
            record.encrypted_secret = b""
            record.encrypted_dek = b""
            record.status = CredentialStatus.REVOKED.value
            record.updated_at = datetime.now(timezone.utc)
            await self._write_audit_log(
                session,
                actor_id=actor_id,
                action="REVOKE_CREDENTIAL",
                credential_id=str(record.id),
                provider_id=record.provider_id,
                details={"status": CredentialStatus.REVOKED.value},
            )
            await session.commit()
            await session.refresh(record)
            return self._to_public_view(record)

    async def delete_credential(
        self, credential_id: str, *, actor_id: str = "admin"
    ) -> None:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            provider_id = record.provider_id
            await self._write_audit_log(
                session,
                actor_id=actor_id,
                action="DELETE_CREDENTIAL",
                credential_id=str(record.id),
                provider_id=provider_id,
                details={"label": record.label, "fingerprint": record.key_fingerprint},
            )
            await session.delete(record)
            await session.commit()
        await self._redis_manager.client.delete(
            f"ugw:cd:cred:{credential_id}",
            f"ugw:inflight:cred:{credential_id}",
            f"ugw:cred:{credential_id}:state",
        )

    async def validate_credential(
        self, credential_id: str, *, actor_id: str = "admin"
    ) -> HealthCheckResult:
        """Decrypts the credential in memory and probes the upstream provider to verify validity."""
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            adapter_cred = self._decrypt_to_adapter_credential(record)
            adapter = self._provider_registry.get_adapter(record.provider_id)

        health = await adapter.check_health(credential=adapter_cred)
        if health.status == HealthStatus.HEALTHY:
            await self._transition_status(
                credential_id,
                CredentialStatus.ACTIVE,
                actor_id=actor_id,
                action="VALIDATE_CREDENTIAL_SUCCESS",
            )
        elif health.details.get("status_code") == 401:
            await self._transition_status(
                credential_id,
                CredentialStatus.INVALID,
                actor_id=actor_id,
                action="VALIDATE_CREDENTIAL_INVALID",
                reason=health.message,
            )
        elif health.details.get("status_code") in (402, 403):
            await self._transition_status(
                credential_id,
                CredentialStatus.REQUIRES_ADMIN_ATTENTION,
                actor_id=actor_id,
                action="VALIDATE_CREDENTIAL_ATTENTION",
                reason=health.message,
            )
        return health

    async def get_credential_health(
        self, credential_id: str, *, model: str | None = None
    ) -> CredentialHealthView:
        async with self._db.session_factory() as session:
            record = await self._fetch_record(session, credential_id)
            acct_rec: AccountGroupRecord | None = None
            if record.account_group_id:
                acct_rec = await session.get(
                    AccountGroupRecord, record.account_group_id
                )

        adapter = self._provider_registry.get_adapter(record.provider_id)
        manifest = adapter.get_capability_manifest()
        cred_quota = QuotaConfigSpec(**(record.quota_config or {}))
        acct_quota = (
            QuotaConfigSpec(**(acct_rec.quota_config or {})) if acct_rec else None
        )
        acct_id_str = str(record.account_group_id) if record.account_group_id else None

        eligibility = await self._quota_manager.check_eligibility(
            provider_id=record.provider_id,
            supported_scopes=manifest.supported_quota_scopes,
            credential_id=str(record.id),
            account_group_id=acct_id_str,
            workspace_id=record.workspace_id,
            cred_max_concurrency=record.max_concurrency,
            acct_max_concurrency=acct_rec.max_concurrency if acct_rec else None,
            cred_quota=cred_quota,
            acct_quota=acct_quota,
            model=model,
        )

        redis = self._redis_manager.client
        state_hash = await redis.hgetall(f"ugw:cred:{credential_id}:state")
        consec_failures = int(state_hash.get("consecutive_failures", 0))
        failed_auth = int(
            await redis.get(f"ugw:stats:cred:{credential_id}:auth_failures") or 0
        )
        rl_events = int(
            await redis.get(f"ugw:stats:cred:{credential_id}:ratelimits") or 0
        )

        persistent_status = CredentialStatus(record.status)
        effective_status = persistent_status
        if persistent_status == CredentialStatus.ACTIVE:
            if eligibility.cooldown_remaining_seconds > 0:
                effective_status = CredentialStatus.COOLING_DOWN
            elif not eligibility.eligible:
                effective_status = CredentialStatus.TEMPORARILY_UNAVAILABLE

        return CredentialHealthView(
            credential_id=str(record.id),
            provider_id=record.provider_id,
            account_group_id=acct_id_str,
            workspace_id=record.workspace_id,
            persistent_status=persistent_status,
            effective_status=effective_status,
            is_eligible=(
                persistent_status == CredentialStatus.ACTIVE and eligibility.eligible
            ),
            active_requests=eligibility.active_requests,
            max_concurrency=record.max_concurrency,
            account_active_requests=eligibility.account_active_requests,
            account_max_concurrency=acct_rec.max_concurrency if acct_rec else None,
            cooldown_remaining_seconds=eligibility.cooldown_remaining_seconds,
            cooldown_scope=eligibility.blocking_scope,
            consecutive_failures=consec_failures,
            failed_auth_events=failed_auth,
            rate_limit_events=rl_events,
            remaining_requests=eligibility.remaining_requests,
            remaining_tokens=eligibility.remaining_tokens,
            last_used_ms=eligibility.last_used_ms,
            supported_quota_scopes=manifest.supported_quota_scopes,
        )

    # =========================================================================
    # Credential Selection & Reservation Algorithms
    # =========================================================================
    async def select_and_reserve_credential(
        self,
        provider_id: str,
        *,
        strategy: SelectionStrategy = SelectionStrategy.RATE_LIMIT_AWARE,
        model: str | None = None,
        endpoint: str | None = None,
        estimated_tokens: int = 0,
        excluded_credential_ids: set[str] | None = None,
        request_id: str | None = None,
    ) -> CredentialLease:
        """Atomically selects an eligible credential using the specified algorithm and reserves a slot."""
        req_id = request_id or str(uuid.uuid4())
        excluded = excluded_credential_ids or set()
        adapter = self._provider_registry.get_adapter(provider_id)
        supported_scopes = adapter.get_capability_manifest().supported_quota_scopes

        async with self._selection_lock:
            async with self._db.session_factory() as session:
                stmt = select(CredentialRecord).where(
                    CredentialRecord.provider_id == provider_id,
                    CredentialRecord.status == CredentialStatus.ACTIVE.value,
                )
                records = list((await session.execute(stmt)).scalars().all())
                if not records:
                    raise ServiceUnavailableError(
                        f"No active credentials configured for provider {provider_id!r}",
                        details={"provider_id": provider_id},
                    )

                acct_ids = {
                    r.account_group_id for r in records if r.account_group_id is not None
                }
                acct_map: dict[uuid.UUID, AccountGroupRecord] = {}
                if acct_ids:
                    acct_rows = (
                        await session.execute(
                            select(AccountGroupRecord).where(
                                AccountGroupRecord.id.in_(acct_ids)
                            )
                        )
                    ).scalars().all()
                    acct_map = {a.id: a for a in acct_rows}

            eligible_candidates: list[
                tuple[CredentialRecord, QuotaEligibilityResult]
            ] = []
            for rec in records:
                cid = str(rec.id)
                if cid in excluded:
                    continue
                if (
                    rec.allowed_models
                    and model
                    and model not in rec.allowed_models
                    and f"{provider_id}/{model}" not in rec.allowed_models
                ):
                    continue

                acct_rec = (
                    acct_map.get(rec.account_group_id)
                    if rec.account_group_id
                    else None
                )
                cred_quota = QuotaConfigSpec(**(rec.quota_config or {}))
                acct_quota = (
                    QuotaConfigSpec(**(acct_rec.quota_config or {}))
                    if acct_rec
                    else None
                )

                elig = await self._quota_manager.check_eligibility(
                    provider_id=provider_id,
                    supported_scopes=supported_scopes,
                    credential_id=cid,
                    account_group_id=str(rec.account_group_id)
                    if rec.account_group_id
                    else None,
                    workspace_id=rec.workspace_id,
                    cred_max_concurrency=rec.max_concurrency,
                    acct_max_concurrency=acct_rec.max_concurrency
                    if acct_rec
                    else None,
                    cred_quota=cred_quota,
                    acct_quota=acct_quota,
                    model=model,
                    endpoint=endpoint,
                    estimated_tokens=estimated_tokens,
                )
                if elig.eligible:
                    eligible_candidates.append((rec, elig))

            if not eligible_candidates:
                raise RateLimitExceededError(
                    f"All active credentials for provider {provider_id!r} are cooling down or at quota/concurrency capacity",
                    details={"provider_id": provider_id},
                )

            selected_rec = await self._apply_selection_strategy(
                provider_id=provider_id,
                strategy=strategy,
                candidates=eligible_candidates,
            )

            cand_acct = (
                acct_map.get(selected_rec.account_group_id)
                if selected_rec.account_group_id
                else None
            )
            cand_cred_quota = QuotaConfigSpec(**(selected_rec.quota_config or {}))
            cand_acct_quota = (
                QuotaConfigSpec(**(cand_acct.quota_config or {}))
                if cand_acct
                else None
            )
            acct_id_str = (
                str(selected_rec.account_group_id)
                if selected_rec.account_group_id
                else None
            )
            reserved, _ = await self._quota_manager.try_acquire_lease_atomic(
                request_id=req_id,
                provider_id=provider_id,
                supported_scopes=supported_scopes,
                credential_id=str(selected_rec.id),
                account_group_id=acct_id_str,
                workspace_id=selected_rec.workspace_id,
                cred_max_concurrency=selected_rec.max_concurrency,
                acct_max_concurrency=cand_acct.max_concurrency if cand_acct else None,
                cred_quota=cand_cred_quota,
                acct_quota=cand_acct_quota,
                model=model,
                endpoint=endpoint,
                estimated_tokens=estimated_tokens,
            )
            if not reserved:
                raise RateLimitExceededError(
                    f"Selected credential for provider {provider_id!r} could not acquire atomic lease",
                    details={"provider_id": provider_id},
                )

            adapter_cred = self._decrypt_to_adapter_credential(selected_rec)
            return CredentialLease(
                request_id=req_id,
                credential=adapter_cred,
                account_group_id=str(selected_rec.account_group_id) if selected_rec.account_group_id else None,
                workspace_id=selected_rec.workspace_id,
                label=selected_rec.label,
                masked_preview=selected_rec.masked_preview,
            )

    async def _rank_candidates_by_strategy(
        self,
        *,
        provider_id: str,
        strategy: SelectionStrategy,
        candidates: list[tuple[CredentialRecord, QuotaEligibilityResult]],
    ) -> list[tuple[CredentialRecord, QuotaEligibilityResult]]:
        ordered = sorted(candidates, key=lambda item: str(item[0].id))
        if len(ordered) <= 1:
            return ordered

        if strategy == SelectionStrategy.ROUND_ROBIN:
            idx = await self._quota_manager.next_round_robin_index(f"{provider_id}:rr")
            start = idx % len(ordered)
            return ordered[start:] + ordered[:start]

        if strategy == SelectionStrategy.WEIGHTED:
            # Sort descending by weight
            return sorted(ordered, key=lambda c: -c[0].weight)

        if strategy == SelectionStrategy.LEAST_RECENTLY_USED:
            return sorted(ordered, key=lambda item: (item[1].last_used_ms or 0, str(item[0].id)))

        # Default RATE_LIMIT_AWARE
        def rate_limit_score(item: tuple[CredentialRecord, QuotaEligibilityResult]) -> float:
            rec, elig = item
            rem_req = elig.remaining_requests if elig.remaining_requests is not None else 1000
            max_conc = rec.max_concurrency or 100
            avail_conc_ratio = max(max_conc - elig.active_requests, 0) / max_conc
            return (rem_req * 0.7) + (avail_conc_ratio * 100.0) + (rec.weight * 0.2)

        return sorted(ordered, key=rate_limit_score, reverse=True)

    async def _apply_selection_strategy(
        self,
        *,
        provider_id: str,
        strategy: SelectionStrategy,
        candidates: list[tuple[CredentialRecord, QuotaEligibilityResult]],
    ) -> CredentialRecord:
        # Sort deterministically by ID first
        ordered = sorted(candidates, key=lambda item: str(item[0].id))
        if len(ordered) == 1:
            return ordered[0][0]

        if strategy == SelectionStrategy.ROUND_ROBIN:
            idx = await self._quota_manager.next_round_robin_index(
                f"{provider_id}:rr"
            )
            return ordered[idx % len(ordered)][0]

        if strategy == SelectionStrategy.WEIGHTED:
            total_weight = sum(max(c[0].weight, 1) for c in ordered)
            tick = await self._quota_manager.next_round_robin_index(
                f"{provider_id}:weighted"
            )
            target = tick % total_weight
            running = 0
            for rec, _ in ordered:
                running += max(rec.weight, 1)
                if target < running:
                    return rec
            return ordered[0][0]

        if strategy == SelectionStrategy.LEAST_ACTIVE:
            return min(
                ordered,
                key=lambda item: (
                    item[1].active_requests
                    / max(item[0].max_concurrency or 100, 1),
                    item[1].active_requests,
                    item[1].last_used_ms or 0,
                ),
            )[0]

        if strategy == SelectionStrategy.LEAST_RECENTLY_USED:
            return min(
                ordered,
                key=lambda item: (item[1].last_used_ms or 0, str(item[0].id)),
            )[0]

        # Default: RATE_LIMIT_AWARE
        def rate_limit_score(
            item: tuple[CredentialRecord, QuotaEligibilityResult]
        ) -> tuple[float, int, int]:
            rec, elig = item
            rem_req = (
                elig.remaining_requests
                if elig.remaining_requests is not None
                else 1000
            )
            max_conc = rec.max_concurrency or 100
            avail_conc_ratio = max(max_conc - elig.active_requests, 0) / max_conc
            composite = (rem_req * 0.7) + (avail_conc_ratio * 100.0) + (rec.weight * 0.2)
            return (composite, -elig.active_requests, -(elig.last_used_ms or 0))

        return max(ordered, key=rate_limit_score)[0]

    # =========================================================================
    # Execution with Rotation, Cooldowns, and Bounded Retry
    # =========================================================================
    async def execute_with_rotation(
        self,
        provider_id: str,
        operation: Callable[[AdapterCredential], Awaitable[T]],
        *,
        usage_extractor: Callable[[T], tuple[TokenUsage | None, RateLimitSnapshot | None]]
        | None = None,
        strategy: SelectionStrategy = SelectionStrategy.RATE_LIMIT_AWARE,
        model: str | None = None,
        endpoint: str | None = None,
        estimated_tokens: int = 0,
    ) -> tuple[T, CredentialLease]:
        """Executes an upstream provider call with automatic credential selection, quota tracking, and safe retries."""
        adapter = self._provider_registry.get_adapter(provider_id)
        supported_scopes = adapter.get_capability_manifest().supported_quota_scopes
        excluded_creds: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(self._retry_policy.max_attempts):
            lease = await self.select_and_reserve_credential(
                provider_id=provider_id,
                strategy=strategy,
                model=model,
                endpoint=endpoint,
                estimated_tokens=estimated_tokens,
                excluded_credential_ids=excluded_creds,
            )
            cred_id = lease.credential.credential_id
            usage: TokenUsage | None = None
            rate_limit: RateLimitSnapshot | None = None

            try:
                result = await operation(lease.credential)
                if usage_extractor is not None:
                    usage, rate_limit = usage_extractor(result)

                await self._redis_manager.client.hset(
                    f"ugw:cred:{cred_id}:state",
                    mapping={"consecutive_failures": "0"},
                )
                await self._record_usage(
                    lease=lease,
                    provider_id=provider_id,
                    model=model,
                    endpoint=endpoint,
                    usage=usage,
                    status_code=200,
                    error_type=None,
                )
                return result, lease

            except UpstreamProviderError as exc:
                last_error = exc
                await self._record_usage(
                    lease=lease,
                    provider_id=provider_id,
                    model=model,
                    endpoint=endpoint,
                    usage=None,
                    status_code=exc.status_code,
                    error_type=exc.error_type,
                )
                retry_after = exc.details.get("retry_after_seconds")
                scope_str = exc.details.get("quota_scope", QuotaScopeType.CREDENTIAL.value)
                try:
                    scope = QuotaScopeType(scope_str)
                except ValueError:
                    scope = QuotaScopeType.CREDENTIAL

                # 1. Authentication Error (401) -> Mark credential INVALID, do not reuse this credential
                if exc.status_code == 401 or exc.details.get(
                    "requires_credential_disable"
                ):
                    await self._redis_manager.client.incr(
                        f"ugw:stats:cred:{cred_id}:auth_failures"
                    )
                    await self._transition_status(
                        cred_id,
                        CredentialStatus.INVALID,
                        action="AUTO_DISABLE_INVALID_AUTH",
                        reason=exc.message,
                    )
                    excluded_creds.add(cred_id)
                    continue

                # 2. Billing / Permission / Hard Quota Exhausted (402 / 403 / insufficient_quota)
                if exc.details.get("requires_admin_attention") or exc.status_code in (
                    402,
                    403,
                ):
                    await self._transition_status(
                        cred_id,
                        CredentialStatus.REQUIRES_ADMIN_ATTENTION,
                        action="AUTO_FLAG_ADMIN_ATTENTION",
                        reason=exc.message,
                    )
                    if scope in (QuotaScopeType.ACCOUNT, QuotaScopeType.WORKSPACE):
                        await self._quota_manager.set_cooldown(
                            provider_id=provider_id,
                            supported_scopes=supported_scopes,
                            scope=scope,
                            duration_seconds=retry_after or 60.0,
                            credential_id=cred_id,
                            account_group_id=lease.account_group_id,
                            workspace_id=lease.workspace_id,
                            model=model,
                            endpoint=endpoint,
                        )
                    excluded_creds.add(cred_id)
                    if exc.status_code == 403 and not exc.retryable:
                        raise AuthenticationError(
                            exc.message,
                            status_code=403,
                            error_code=exc.error_code,
                        ) from exc
                    continue

                # 3. Rate Limit (429) -> Set scope-aware cooldown (Credential, Account, Workspace, Model, Concurrency)
                if exc.status_code == 429 or exc.error_type == "rate_limit_error":
                    await self._quota_manager.set_cooldown(
                        provider_id=provider_id,
                        supported_scopes=supported_scopes,
                        scope=scope,
                        duration_seconds=float(retry_after or 5.0),
                        credential_id=cred_id,
                        account_group_id=lease.account_group_id,
                        workspace_id=lease.workspace_id,
                        model=model,
                        endpoint=endpoint,
                    )
                    excluded_creds.add(cred_id)
                    if attempt + 1 < self._retry_policy.max_attempts:
                        delay = self._retry_policy.compute_backoff_seconds(
                            attempt, retry_after_seconds=retry_after
                        )
                        await asyncio.sleep(delay)
                    continue

                # 4. Permanent Invalid Request (400, 404, 422) -> Never retry!
                if not exc.retryable or exc.status_code in (400, 404, 422):
                    raise

                # 5. Retryable 5xx Server Error
                await self._handle_transient_failure(cred_id, exc.message)
                excluded_creds.add(cred_id)
                if attempt + 1 < self._retry_policy.max_attempts:
                    delay = self._retry_policy.compute_backoff_seconds(
                        attempt, retry_after_seconds=retry_after
                    )
                    await asyncio.sleep(delay)

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                err_kind = (
                    "timeout_error"
                    if isinstance(exc, httpx.TimeoutException)
                    else "connection_error"
                )
                await self._record_usage(
                    lease=lease,
                    provider_id=provider_id,
                    model=model,
                    endpoint=endpoint,
                    usage=None,
                    status_code=504 if err_kind == "timeout_error" else 502,
                    error_type=err_kind,
                )
                await self._handle_transient_failure(cred_id, str(exc))
                excluded_creds.add(cred_id)
                if attempt + 1 < self._retry_policy.max_attempts:
                    delay = self._retry_policy.compute_backoff_seconds(attempt)
                    await asyncio.sleep(delay)

            finally:
                await self._quota_manager.release_lease(
                    request_id=lease.request_id,
                    credential_id=cred_id,
                    account_group_id=lease.account_group_id,
                    usage=usage,
                    rate_limit=rate_limit,
                )

        if isinstance(last_error, UpstreamProviderError):
            raise last_error
        if isinstance(last_error, httpx.TimeoutException):
            raise UpstreamProviderError(
                f"Upstream request timed out after {self._retry_policy.max_attempts} attempts",
                status_code=504,
                error_code="GW_UPSTREAM_TIMEOUT",
                retryable=True,
            ) from last_error
        raise UpstreamProviderError(
            f"Upstream execution failed after {self._retry_policy.max_attempts} attempts: {last_error}",
            status_code=502,
            error_code="GW_UPSTREAM_CONNECTION_ERROR",
            retryable=True,
        ) from last_error

    async def _handle_transient_failure(
        self, credential_id: str, reason: str
    ) -> None:
        redis = self._redis_manager.client
        state_key = f"ugw:cred:{credential_id}:state"
        cur = int(await redis.hget(state_key, "consecutive_failures") or 0) + 1
        await redis.hset(state_key, mapping={"consecutive_failures": str(cur)})
        if cur >= 3:
            await self._transition_status(
                credential_id,
                CredentialStatus.TEMPORARILY_UNAVAILABLE,
                action="AUTO_MARK_TEMPORARILY_UNAVAILABLE",
                reason=f"{cur} consecutive failures: {reason}",
            )

    async def _record_usage(
        self,
        *,
        lease: CredentialLease,
        provider_id: str,
        model: str | None,
        endpoint: str | None,
        usage: TokenUsage | None,
        status_code: int,
        error_type: str | None,
    ) -> None:
        async with self._db.session_factory() as session:
            rec = CredentialUsageRecord(
                credential_id=uuid.UUID(lease.credential.credential_id),
                account_group_id=uuid.UUID(lease.account_group_id)
                if lease.account_group_id
                else None,
                workspace_id=lease.workspace_id,
                provider_id=provider_id,
                model=model,
                endpoint=endpoint,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
                characters=usage.characters if usage else 0,
                status_code=status_code,
                error_type=error_type,
            )
            session.add(rec)
            await session.commit()

    async def get_usage_summary(
        self,
        *,
        credential_id: str | None = None,
        account_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Returns aggregated request and token/character usage for a credential or account group."""
        async with self._db.session_factory() as session:
            stmt = select(CredentialUsageRecord)
            if credential_id:
                stmt = stmt.where(
                    CredentialUsageRecord.credential_id == uuid.UUID(credential_id)
                )
            if account_group_id:
                stmt = stmt.where(
                    CredentialUsageRecord.account_group_id
                    == uuid.UUID(account_group_id)
                )
            rows = (await session.execute(stmt)).scalars().all()
            return {
                "total_requests": len(rows),
                "successful_requests": sum(
                    1 for r in rows if 200 <= r.status_code < 400
                ),
                "failed_requests": sum(1 for r in rows if r.status_code >= 400),
                "input_tokens": sum(r.input_tokens for r in rows),
                "output_tokens": sum(r.output_tokens for r in rows),
                "total_tokens": sum(r.total_tokens for r in rows),
                "characters": sum(r.characters for r in rows),
            }

    async def list_audit_logs(
        self, credential_id: str | None = None
    ) -> list[CredentialAuditEntryView]:
        async with self._db.session_factory() as session:
            stmt = select(CredentialAuditLogRecord).order_by(
                CredentialAuditLogRecord.created_at.desc()
            )
            if credential_id:
                stmt = stmt.where(
                    CredentialAuditLogRecord.credential_id == credential_id
                )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                CredentialAuditEntryView(
                    id=str(r.id),
                    actor_id=r.actor_id,
                    action=r.action,
                    credential_id=r.credential_id,
                    provider_id=r.provider_id,
                    details=r.details_json,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def _fetch_record(
        self, session: Any, credential_id: str
    ) -> CredentialRecord:
        try:
            cred_uuid = uuid.UUID(credential_id)
        except ValueError as exc:
            raise ResourceNotFoundError(
                f"Invalid credential UUID {credential_id!r}", param="credential_id"
            ) from exc
        record = await session.get(CredentialRecord, cred_uuid)
        if record is None:
            raise ResourceNotFoundError(
                f"Credential {credential_id!r} not found", param="credential_id"
            )
        return record

    def _decrypt_to_adapter_credential(
        self, record: CredentialRecord
    ) -> AdapterCredential:
        plaintext = self._encryptor.decrypt(
            record.encrypted_secret,
            record.encrypted_dek,
            provider_id=record.provider_id,
            credential_id=str(record.id),
        )
        return AdapterCredential(
            credential_id=str(record.id),
            provider_id=record.provider_id,
            account_group_id=str(record.account_group_id)
            if record.account_group_id
            else None,
            secret=SecretStr(plaintext),
            extra_headers=record.extra_headers or {},
            organization_id=record.organization_id,
            project_id=record.project_id,
        )

