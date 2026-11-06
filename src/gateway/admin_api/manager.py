"""Manager for Client Gateway API Keys (`ugw_live_...`) and System Control Plane Overview."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from sqlalchemy import desc, select

from gateway.admin_api.models import (
    ClientApiKeyCreatedResponse,
    ClientApiKeyCreateRequest,
    ClientApiKeyORM,
    ClientApiKeyUpdateRequest,
    ClientApiKeyView,
    ControlPlaneEntityORM,
    SystemSettingsView,
)
from gateway.config.settings import GatewaySettings
from gateway.core.db import Base, DatabaseManager
from gateway.core.exceptions import (
    AuthenticationError,
    RateLimitExceededError,
    ResourceNotFoundError,
)
from gateway.core.redis import RedisManager


class AdminControlPlaneManager:
    """Manages Client Gateway API keys and aggregates control-plane state for the Admin Dashboard."""

    def __init__(
        self,
        settings: GatewaySettings,
        db_manager: DatabaseManager,
        redis_manager: RedisManager,
    ) -> None:
        self._settings = settings
        self._db_manager = db_manager
        self._redis_manager = redis_manager

    async def ensure_tables(self) -> None:
        """Creates the `gateway_client_api_keys` table if not present."""
        engine = self._db_manager.engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    async def create_client_key(
        self, payload: ClientApiKeyCreateRequest
    ) -> ClientApiKeyCreatedResponse:
        """Generates a high-entropy `ugw_live_...` client key, stores only its SHA-256 hash, and returns the raw key once."""
        token_body = secrets.token_urlsafe(24)
        raw_key = f"ugw_live_{token_body}"
        prefix = f"{raw_key[:14]}...{raw_key[-4:]}"
        key_hash = self._hash_key(raw_key)

        orm_obj = ClientApiKeyORM(
            name=payload.name,
            key_prefix=prefix,
            key_hash=key_hash,
            role=payload.role,
            allowed_providers_csv=",".join(sorted(set(payload.allowed_providers))),
            allowed_tools_csv=",".join(sorted(set(payload.allowed_tools))),
            rate_limit_rpm=payload.rate_limit_rpm,
            monthly_budget_usd=payload.monthly_budget_usd,
            enabled=True,
        )
        async with self._db_manager.session() as session:
            session.add(orm_obj)
            await session.flush()
            await session.refresh(orm_obj)
            view = self._to_view(orm_obj)
            return ClientApiKeyCreatedResponse(
                **view.model_dump(),
                api_key=raw_key,
            )

    async def list_client_keys(self) -> list[ClientApiKeyView]:
        async with self._db_manager.session() as session:
            stmt = select(ClientApiKeyORM).order_by(desc(ClientApiKeyORM.created_at))
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_view(r) for r in rows]

    async def authenticate_client_key(self, raw_key: str) -> ClientApiKeyView:
        """Validates a raw `ugw_live_...` key and consumes one unit of its per-minute rate limit.

        Only the SHA-256 hash is ever compared (lookup on the indexed hash column plus a
        constant-time re-check), so raw keys are never stored or logged.
        """
        key_hash = self._hash_key(raw_key)
        async with self._db_manager.session() as session:
            stmt = select(ClientApiKeyORM).where(ClientApiKeyORM.key_hash == key_hash)
            row = (await session.execute(stmt)).scalars().first()
            if (
                row is None
                or not hmac.compare_digest(row.key_hash, key_hash)
                or not row.enabled
            ):
                raise AuthenticationError(
                    "Invalid or revoked Gateway client API key",
                    error_code="GW_CLIENT_KEY_INVALID",
                )
            view = self._to_view(row)

        if view.rate_limit_rpm and view.rate_limit_rpm > 0:
            minute_bucket = int(time.time() // 60)
            rpm_key = f"ugw:client_key:{view.id}:rpm:{minute_bucket}"
            redis = self._redis_manager.client
            used = await redis.incr(rpm_key)
            if used == 1:
                await redis.expire(rpm_key, 120)
            if used > view.rate_limit_rpm:
                raise RateLimitExceededError(
                    f"Client API key rate limit ({view.rate_limit_rpm} RPM) exceeded",
                    error_code="GW_CLIENT_KEY_RATE_LIMITED",
                    details={"rate_limit_rpm": view.rate_limit_rpm},
                )
        return view

    async def revoke_client_key(self, key_id: str) -> ClientApiKeyView:
        try:
            uid = uuid.UUID(key_id)
        except (ValueError, AttributeError) as exc:
            raise ResourceNotFoundError(f"Client API key {key_id!r} not found") from exc

        async with self._db_manager.session() as session:
            row = await session.get(ClientApiKeyORM, uid)
            if row is None:
                raise ResourceNotFoundError(f"Client API key {key_id!r} not found")
            row.enabled = False
            await session.flush()
            await session.refresh(row)
            return self._to_view(row)

    async def delete_client_key(self, key_id: str) -> None:
        try:
            uid = uuid.UUID(key_id)
        except (ValueError, AttributeError) as exc:
            raise ResourceNotFoundError(f"Client API key {key_id!r} not found") from exc

        async with self._db_manager.session() as session:
            row = await session.get(ClientApiKeyORM, uid)
            if row is None:
                raise ResourceNotFoundError(f"Client API key {key_id!r} not found")
            await session.delete(row)

    async def update_client_key(
        self, key_id: str, payload: ClientApiKeyUpdateRequest
    ) -> ClientApiKeyView:
        try:
            uid = uuid.UUID(key_id)
        except (ValueError, AttributeError) as exc:
            raise ResourceNotFoundError(f"Client API key {key_id!r} not found") from exc

        async with self._db_manager.session() as session:
            row = await session.get(ClientApiKeyORM, uid)
            if row is None:
                raise ResourceNotFoundError(f"Client API key {key_id!r} not found")

            if payload.name is not None:
                row.name = payload.name
            if payload.role is not None:
                row.role = payload.role
            if payload.allowed_providers is not None:
                row.allowed_providers_csv = ",".join(payload.allowed_providers)
            if payload.allowed_tools is not None:
                row.allowed_tools_csv = ",".join(payload.allowed_tools)
            if payload.rate_limit_rpm is not None:
                row.rate_limit_rpm = payload.rate_limit_rpm
            if payload.monthly_budget_usd is not None:
                row.monthly_budget_usd = payload.monthly_budget_usd
            if payload.enabled is not None:
                row.enabled = payload.enabled

            await session.flush()
            await session.refresh(row)
            return self._to_view(row)

    async def persist_entity(
        self, entity_type: str, entity_id: str, payload_json: str
    ) -> None:
        """Persists or updates a control-plane entity in the gateway_control_plane_entities table."""
        async with self._db_manager.session() as session:
            stmt = select(ControlPlaneEntityORM).where(
                ControlPlaneEntityORM.entity_type == entity_type,
                ControlPlaneEntityORM.entity_id == entity_id,
            )
            existing = (await session.execute(stmt)).scalars().first()
            if existing:
                existing.payload_json = payload_json
            else:
                session.add(
                    ControlPlaneEntityORM(
                        entity_type=entity_type,
                        entity_id=entity_id,
                        payload_json=payload_json,
                    )
                )

    async def list_persisted_entities(
        self, entity_type: str | None = None
    ) -> list[ControlPlaneEntityORM]:
        """Lists persisted control-plane entities, optionally filtered by entity_type."""
        async with self._db_manager.session() as session:
            stmt = select(ControlPlaneEntityORM)
            if entity_type:
                stmt = stmt.where(ControlPlaneEntityORM.entity_type == entity_type)
            return list((await session.execute(stmt)).scalars().all())

    async def delete_persisted_entity(self, entity_type: str, entity_id: str) -> None:
        """Deletes a persisted control-plane entity."""
        async with self._db_manager.session() as session:
            stmt = select(ControlPlaneEntityORM).where(
                ControlPlaneEntityORM.entity_type == entity_type,
                ControlPlaneEntityORM.entity_id == entity_id,
            )
            existing = (await session.execute(stmt)).scalars().first()
            if existing:
                await session.delete(existing)

    async def get_system_settings(self) -> SystemSettingsView:
        db_health = await self._db_manager.check_health()
        redis_health = await self._redis_manager.check_health()
        return SystemSettingsView(
            app_name=self._settings.app_name,
            version=self._settings.version,
            environment=self._settings.env,
            api_v1_prefix=self._settings.api_v1_prefix,
            database_dialect=str(db_health.get("dialect", "unknown")),
            redis_backend=str(redis_health.get("backend", "unknown")),
            default_routing_policy="balanced",
            default_routing_weights={
                "quality": self._settings.default_weight_quality,
                "reliability": self._settings.default_weight_reliability,
                "cost": self._settings.default_weight_cost,
                "latency": self._settings.default_weight_latency,
                "capacity": self._settings.default_weight_capacity,
            },
            http_timeout_connect_seconds=self._settings.http_connect_timeout_seconds,
            http_timeout_read_seconds=self._settings.http_read_timeout_seconds,
            http_timeout_write_seconds=self._settings.http_write_timeout_seconds,
            http_timeout_pool_seconds=self._settings.http_pool_timeout_seconds,
            encryption_algorithm="AES-256-GCM",
            secret_redaction_enabled=True,
            ssrf_protection_enabled=True,
            extra_metadata={
                "db_status": db_health.get("status", "unknown"),
                "redis_status": redis_health.get("status", "unknown"),
            },
        )

    def _to_view(self, row: ClientApiKeyORM) -> ClientApiKeyView:
        provs = [p for p in row.allowed_providers_csv.split(",") if p]
        tools = [t for t in row.allowed_tools_csv.split(",") if t]
        return ClientApiKeyView(
            id=str(row.id),
            name=row.name,
            key_prefix=row.key_prefix,
            role=row.role,
            allowed_providers=provs,
            allowed_tools=tools,
            rate_limit_rpm=row.rate_limit_rpm,
            monthly_budget_usd=row.monthly_budget_usd,
            enabled=row.enabled,
            created_at=row.created_at,
        )

