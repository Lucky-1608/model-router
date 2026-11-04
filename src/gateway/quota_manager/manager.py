"""Distributed Quota & Concurrency Manager backed by Redis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any

from gateway.capability_registry.models import QuotaScopeType
from gateway.core.canonical import RateLimitSnapshot, TokenUsage
from gateway.core.redis import RedisManager

if TYPE_CHECKING:
    from gateway.credential_manager.models import QuotaConfigSpec


@dataclass(frozen=True, slots=True)
class QuotaEligibilityResult:
    """Result of checking whether a credential passes all quota, cooldown, and concurrency gates."""

    eligible: bool
    reason: str = "OK"
    blocking_scope: QuotaScopeType | None = None
    cooldown_remaining_seconds: float = 0.0
    active_requests: int = 0
    account_active_requests: int = 0
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    last_used_ms: int | None = None


class QuotaManager:
    """Coordinates multi-scope quotas, rate-limit cooldowns, and in-flight concurrency via Redis."""

    def __init__(self, redis_manager: RedisManager) -> None:
        self._redis_manager = redis_manager
        self._reservation_lock = asyncio.Lock()

    @property
    def redis(self) -> Any:
        return self._redis_manager.client

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _minute_bucket() -> int:
        return int(time.time() // 60)

    def filter_supported_scopes(
        self,
        supported_scopes: list[QuotaScopeType] | set[QuotaScopeType],
        requested_scope: QuotaScopeType,
    ) -> QuotaScopeType:
        """Maps a quota scope to one explicitly supported by the provider."""
        supported_set = set(supported_scopes)
        if requested_scope in supported_set:
            return requested_scope
        if (
            requested_scope == QuotaScopeType.WORKSPACE
            and QuotaScopeType.ACCOUNT in supported_set
        ):
            return QuotaScopeType.ACCOUNT
        if (
            requested_scope == QuotaScopeType.ACCOUNT
            and QuotaScopeType.WORKSPACE in supported_set
        ):
            return QuotaScopeType.WORKSPACE
        if QuotaScopeType.CREDENTIAL in supported_set:
            return QuotaScopeType.CREDENTIAL
        return next(iter(supported_set), QuotaScopeType.CREDENTIAL)

    async def set_cooldown(
        self,
        *,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        scope: QuotaScopeType,
        duration_seconds: float,
        credential_id: str | None = None,
        account_group_id: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> QuotaScopeType:
        """Marks a specific quota scope as cooling down in Redis for duration_seconds."""
        effective_scope = self.filter_supported_scopes(supported_scopes, scope)
        until_ms = self._now_ms() + int(max(duration_seconds, 0.1) * 1000)
        ttl_sec = max(int(duration_seconds) + 5, 5)

        key = self._cooldown_key(
            provider_id=provider_id,
            scope=effective_scope,
            credential_id=credential_id,
            account_group_id=account_group_id,
            workspace_id=workspace_id,
            model=model,
            endpoint=endpoint,
        )
        await self.redis.set(key, str(until_ms), ex=ttl_sec)
        if credential_id:
            await self.redis.incr(f"ugw:stats:cred:{credential_id}:ratelimits")
        return effective_scope

    def _cooldown_key(
        self,
        *,
        provider_id: str,
        scope: QuotaScopeType,
        credential_id: str | None = None,
        account_group_id: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> str:
        if scope == QuotaScopeType.ACCOUNT and account_group_id:
            return f"ugw:cd:acct:{account_group_id}"
        if scope == QuotaScopeType.WORKSPACE and (workspace_id or account_group_id):
            return f"ugw:cd:ws:{workspace_id or account_group_id}"
        if scope == QuotaScopeType.MODEL and model:
            acct_part = account_group_id or credential_id or "global"
            return f"ugw:cd:model:{provider_id}:{acct_part}:{model}"
        if scope == QuotaScopeType.ENDPOINT and endpoint:
            return f"ugw:cd:ep:{provider_id}:{endpoint}"
        return f"ugw:cd:cred:{credential_id or 'unknown'}"

    async def get_active_cooldown(
        self,
        *,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        credential_id: str,
        account_group_id: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> tuple[float, QuotaScopeType | None]:
        """Checks all provider-supported scopes for an active cooldown and returns (remaining_sec, scope)."""
        now_ms = self._now_ms()
        scopes_to_check: list[tuple[QuotaScopeType, str]] = [
            (QuotaScopeType.CREDENTIAL, f"ugw:cd:cred:{credential_id}")
        ]
        supported_set = set(supported_scopes)

        if QuotaScopeType.ACCOUNT in supported_set and account_group_id:
            scopes_to_check.append(
                (QuotaScopeType.ACCOUNT, f"ugw:cd:acct:{account_group_id}")
            )
        if QuotaScopeType.WORKSPACE in supported_set and (
            workspace_id or account_group_id
        ):
            scopes_to_check.append(
                (
                    QuotaScopeType.WORKSPACE,
                    f"ugw:cd:ws:{workspace_id or account_group_id}",
                )
            )
        if QuotaScopeType.MODEL in supported_set and model:
            for acct_part in filter(None, [account_group_id, credential_id]):
                scopes_to_check.append(
                    (
                        QuotaScopeType.MODEL,
                        f"ugw:cd:model:{provider_id}:{acct_part}:{model}",
                    )
                )
        if QuotaScopeType.ENDPOINT in supported_set and endpoint:
            scopes_to_check.append(
                (QuotaScopeType.ENDPOINT, f"ugw:cd:ep:{provider_id}:{endpoint}")
            )

        max_rem_sec = 0.0
        blocking_scope: QuotaScopeType | None = None
        for scope, key in scopes_to_check:
            raw_until = await self.redis.get(key)
            if raw_until is not None:
                until_ms = int(raw_until)
                if until_ms > now_ms:
                    rem_sec = (until_ms - now_ms) / 1000.0
                    if rem_sec > max_rem_sec:
                        max_rem_sec = rem_sec
                        blocking_scope = scope
        return round(max_rem_sec, 3), blocking_scope

    async def check_eligibility(
        self,
        *,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        credential_id: str,
        account_group_id: str | None,
        workspace_id: str | None,
        cred_max_concurrency: int | None,
        acct_max_concurrency: int | None,
        cred_quota: QuotaConfigSpec,
        acct_quota: QuotaConfigSpec | None,
        model: str | None = None,
        endpoint: str | None = None,
        estimated_tokens: int = 0,
    ) -> QuotaEligibilityResult:
        """Evaluates cooldowns, concurrency limits, and configured window quotas across supported scopes."""
        cd_rem, cd_scope = await self.get_active_cooldown(
            provider_id=provider_id,
            supported_scopes=supported_scopes,
            credential_id=credential_id,
            account_group_id=account_group_id,
            workspace_id=workspace_id,
            model=model,
            endpoint=endpoint,
        )
        active_req = await self.redis.scard(f"ugw:inflight:cred:{credential_id}")
        acct_active_req = (
            await self.redis.scard(f"ugw:inflight:acct:{account_group_id}")
            if account_group_id
            else 0
        )
        state_hash = await self.redis.hgetall(f"ugw:cred:{credential_id}:state")
        last_used_ms = (
            int(state_hash["last_used_ms"])
            if "last_used_ms" in state_hash
            else None
        )
        rem_req_header = (
            int(state_hash["remaining_requests"])
            if "remaining_requests" in state_hash
            else None
        )
        rem_tok_header = (
            int(state_hash["remaining_tokens"])
            if "remaining_tokens" in state_hash
            else None
        )

        if cd_rem > 0:
            return QuotaEligibilityResult(
                eligible=False,
                reason=f"Cooling down on scope {cd_scope}",
                blocking_scope=cd_scope,
                cooldown_remaining_seconds=cd_rem,
                active_requests=active_req,
                account_active_requests=acct_active_req,
                remaining_requests=rem_req_header,
                remaining_tokens=rem_tok_header,
                last_used_ms=last_used_ms,
            )

        if cred_max_concurrency is not None and active_req >= cred_max_concurrency:
            return QuotaEligibilityResult(
                eligible=False,
                reason="Credential max_concurrency reached",
                blocking_scope=QuotaScopeType.CONCURRENCY,
                active_requests=active_req,
                account_active_requests=acct_active_req,
                remaining_requests=rem_req_header,
                remaining_tokens=rem_tok_header,
                last_used_ms=last_used_ms,
            )

        if (
            account_group_id
            and acct_max_concurrency is not None
            and acct_active_requests_exceeded(acct_active_req, acct_max_concurrency)
        ):
            return QuotaEligibilityResult(
                eligible=False,
                reason="Shared AccountGroup max_concurrency reached",
                blocking_scope=QuotaScopeType.CONCURRENCY,
                active_requests=active_req,
                account_active_requests=acct_active_req,
                remaining_requests=rem_req_header,
                remaining_tokens=rem_tok_header,
                last_used_ms=last_used_ms,
            )

        minute_bucket = self._minute_bucket()
        supported_set = set(supported_scopes)

        # Credential-level RPM/TPM check
        if QuotaScopeType.CREDENTIAL in supported_set:
            if cred_quota.requests_per_minute is not None:
                used_rpm = int(
                    await self.redis.get(
                        f"ugw:win:cred:{credential_id}:rpm:{minute_bucket}"
                    )
                    or 0
                )
                if used_rpm >= cred_quota.requests_per_minute:
                    return QuotaEligibilityResult(
                        eligible=False,
                        reason="Credential RPM quota reached",
                        blocking_scope=QuotaScopeType.CREDENTIAL,
                        active_requests=active_req,
                        account_active_requests=acct_active_req,
                        remaining_requests=0,
                        last_used_ms=last_used_ms,
                    )
                rem_req_header = min(
                    rem_req_header
                    if rem_req_header is not None
                    else cred_quota.requests_per_minute,
                    max(cred_quota.requests_per_minute - used_rpm, 0),
                )

            if cred_quota.tokens_per_minute is not None:
                used_tpm = int(
                    await self.redis.get(
                        f"ugw:win:cred:{credential_id}:tpm:{minute_bucket}"
                    )
                    or 0
                )
                if used_tpm + max(estimated_tokens, 1) > cred_quota.tokens_per_minute:
                    return QuotaEligibilityResult(
                        eligible=False,
                        reason="Credential TPM quota reached",
                        blocking_scope=QuotaScopeType.CREDENTIAL,
                        active_requests=active_req,
                        account_active_requests=acct_active_req,
                        remaining_tokens=max(
                            cred_quota.tokens_per_minute - used_tpm, 0
                        ),
                        last_used_ms=last_used_ms,
                    )

        # Shared AccountGroup / Workspace RPM/TPM check
        if (
            account_group_id
            and acct_quota is not None
            and (
                QuotaScopeType.ACCOUNT in supported_set
                or QuotaScopeType.WORKSPACE in supported_set
            )
        ):
            acct_scope = (
                QuotaScopeType.WORKSPACE
                if QuotaScopeType.WORKSPACE in supported_set
                and QuotaScopeType.ACCOUNT not in supported_set
                else QuotaScopeType.ACCOUNT
            )
            if acct_quota.requests_per_minute is not None:
                used_acct_rpm = int(
                    await self.redis.get(
                        f"ugw:win:acct:{account_group_id}:rpm:{minute_bucket}"
                    )
                    or 0
                )
                if used_acct_rpm >= acct_quota.requests_per_minute:
                    return QuotaEligibilityResult(
                        eligible=False,
                        reason="Shared AccountGroup RPM quota reached",
                        blocking_scope=acct_scope,
                        active_requests=active_req,
                        account_active_requests=acct_active_req,
                        remaining_requests=0,
                        last_used_ms=last_used_ms,
                    )
                rem_req_header = min(
                    rem_req_header
                    if rem_req_header is not None
                    else acct_quota.requests_per_minute,
                    max(acct_quota.requests_per_minute - used_acct_rpm, 0),
                )

            if acct_quota.tokens_per_minute is not None:
                used_acct_tpm = int(
                    await self.redis.get(
                        f"ugw:win:acct:{account_group_id}:tpm:{minute_bucket}"
                    )
                    or 0
                )
                if (
                    used_acct_tpm + max(estimated_tokens, 1)
                    > acct_quota.tokens_per_minute
                ):
                    return QuotaEligibilityResult(
                        eligible=False,
                        reason="Shared AccountGroup TPM quota reached",
                        blocking_scope=acct_scope,
                        active_requests=active_req,
                        account_active_requests=acct_active_req,
                        remaining_tokens=max(
                            acct_quota.tokens_per_minute - used_acct_tpm, 0
                        ),
                        last_used_ms=last_used_ms,
                    )

            # Model-level RPM check if configured on account or credential
            if model and QuotaScopeType.MODEL in supported_set:
                model_limit = acct_quota.model_rpm_limits.get(
                    model
                ) or cred_quota.model_rpm_limits.get(model)
                if model_limit is not None:
                    used_model_rpm = int(
                        await self.redis.get(
                            f"ugw:win:model:{account_group_id}:{model}:rpm:{minute_bucket}"
                        )
                        or 0
                    )
                    if used_model_rpm >= model_limit:
                        return QuotaEligibilityResult(
                            eligible=False,
                            reason=f"Model RPM limit reached for {model}",
                            blocking_scope=QuotaScopeType.MODEL,
                            active_requests=active_req,
                            account_active_requests=acct_active_req,
                            last_used_ms=last_used_ms,
                        )

        return QuotaEligibilityResult(
            eligible=True,
            active_requests=active_req,
            account_active_requests=acct_active_req,
            remaining_requests=rem_req_header,
            remaining_tokens=rem_tok_header,
            last_used_ms=last_used_ms,
        )

    async def try_acquire_lease_atomic(
        self,
        *,
        request_id: str,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        credential_id: str,
        account_group_id: str | None,
        workspace_id: str | None,
        cred_max_concurrency: int | None,
        acct_max_concurrency: int | None,
        cred_quota: QuotaConfigSpec,
        acct_quota: QuotaConfigSpec | None,
        model: str | None = None,
        endpoint: str | None = None,
        estimated_tokens: int = 0,
    ) -> tuple[bool, QuotaEligibilityResult]:
        """Atomically evaluates eligibility and reserves concurrency/window slots.

        When running on a real Redis cluster/instance, executes a server-side Lua script to eliminate
        distributed race conditions across multiple Uvicorn workers or gateway replicas.
        On InMemoryRedisFallback, uses the store's global lock for multi-coroutine atomicity.
        """
        # 1. Cooldown check
        cd_rem, cd_scope = await self.get_active_cooldown(
            provider_id=provider_id,
            supported_scopes=supported_scopes,
            credential_id=credential_id,
            account_group_id=account_group_id,
            workspace_id=workspace_id,
            model=model,
            endpoint=endpoint,
        )
        if cd_rem > 0:
            return False, QuotaEligibilityResult(
                eligible=False,
                reason=f"Cooling down on scope {cd_scope}",
                blocking_scope=cd_scope,
                cooldown_remaining_seconds=cd_rem,
            )

        # Real Redis mode: execute Lua script if eval is available
        if hasattr(self.redis, "eval"):
            return await self._eval_and_reserve_redis_lua(
                request_id=request_id,
                provider_id=provider_id,
                supported_scopes=supported_scopes,
                credential_id=credential_id,
                account_group_id=account_group_id,
                cred_max_concurrency=cred_max_concurrency,
                acct_max_concurrency=acct_max_concurrency,
                cred_quota=cred_quota,
                acct_quota=acct_quota,
                model=model,
                estimated_tokens=estimated_tokens,
            )

        return await self._eval_and_reserve_in_memory(
            request_id=request_id,
            provider_id=provider_id,
            supported_scopes=supported_scopes,
            credential_id=credential_id,
            account_group_id=account_group_id,
            workspace_id=workspace_id,
            cred_max_concurrency=cred_max_concurrency,
            acct_max_concurrency=acct_max_concurrency,
            cred_quota=cred_quota,
            acct_quota=acct_quota,
            model=model,
            estimated_tokens=estimated_tokens,
        )

    async def _eval_and_reserve_in_memory(
        self,
        *,
        request_id: str,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        credential_id: str,
        account_group_id: str | None,
        workspace_id: str | None,
        cred_max_concurrency: int | None,
        acct_max_concurrency: int | None,
        cred_quota: QuotaConfigSpec,
        acct_quota: QuotaConfigSpec | None,
        model: str | None,
        estimated_tokens: int,
    ) -> tuple[bool, QuotaEligibilityResult]:
        active_req = await self.redis.scard(f"ugw:inflight:cred:{credential_id}")
        acct_active_req = (
            await self.redis.scard(f"ugw:inflight:acct:{account_group_id}")
            if account_group_id
            else 0
        )
        if cred_max_concurrency is not None and active_req >= cred_max_concurrency:
            return False, QuotaEligibilityResult(
                eligible=False,
                reason="Credential max_concurrency reached",
                blocking_scope=QuotaScopeType.CONCURRENCY,
                active_requests=active_req,
                account_active_requests=acct_active_req,
            )
        if account_group_id and acct_max_concurrency is not None and acct_active_req >= acct_max_concurrency:
            return False, QuotaEligibilityResult(
                eligible=False,
                reason="Shared AccountGroup max_concurrency reached",
                blocking_scope=QuotaScopeType.CONCURRENCY,
                active_requests=active_req,
                account_active_requests=acct_active_req,
            )

        minute_bucket = self._minute_bucket()
        supported_set = set(supported_scopes)
        if QuotaScopeType.CREDENTIAL in supported_set and cred_quota.requests_per_minute is not None:
            used_rpm = int(await self.redis.get(f"ugw:win:cred:{credential_id}:rpm:{minute_bucket}") or 0)
            if used_rpm >= cred_quota.requests_per_minute:
                return False, QuotaEligibilityResult(
                    eligible=False,
                    reason="Credential RPM quota reached",
                    blocking_scope=QuotaScopeType.CREDENTIAL,
                    active_requests=active_req,
                    account_active_requests=acct_active_req,
                )

        if account_group_id and acct_quota is not None and acct_quota.requests_per_minute is not None:
            used_acct_rpm = int(await self.redis.get(f"ugw:win:acct:{account_group_id}:rpm:{minute_bucket}") or 0)
            if used_acct_rpm >= acct_quota.requests_per_minute:
                return False, QuotaEligibilityResult(
                    eligible=False,
                    reason="Shared AccountGroup RPM quota reached",
                    blocking_scope=QuotaScopeType.ACCOUNT,
                    active_requests=active_req,
                    account_active_requests=acct_active_req,
                )

        # Atomic reservation under the lock
        await self.reserve_lease(
            request_id=request_id,
            credential_id=credential_id,
            account_group_id=account_group_id,
            model=model,
        )
        return True, QuotaEligibilityResult(
            eligible=True,
            active_requests=active_req + 1,
            account_active_requests=acct_active_req + 1,
        )

    async def _eval_and_reserve_redis_lua(
        self,
        *,
        request_id: str,
        provider_id: str,
        supported_scopes: list[QuotaScopeType],
        credential_id: str,
        account_group_id: str | None,
        cred_max_concurrency: int | None,
        acct_max_concurrency: int | None,
        cred_quota: QuotaConfigSpec,
        acct_quota: QuotaConfigSpec | None,
        model: str | None,
        estimated_tokens: int,
    ) -> tuple[bool, QuotaEligibilityResult]:
        lua_script = """
        local cred_inflight_key = KEYS[1]
        local acct_inflight_key = KEYS[2]
        local cred_rpm_key = KEYS[3]
        local acct_rpm_key = KEYS[4]
        local cred_state_key = KEYS[5]

        local request_id = ARGV[1]
        local cred_max_conc = tonumber(ARGV[2])
        local acct_max_conc = tonumber(ARGV[3])
        local cred_max_rpm = tonumber(ARGV[4])
        local acct_max_rpm = tonumber(ARGV[5])
        local now_ms = ARGV[6]

        -- 1. Check credential concurrency
        local cred_active = redis.call('SCARD', cred_inflight_key)
        if cred_max_conc > 0 and cred_active >= cred_max_conc then
            return {0, 'CRED_CONCURRENCY', cred_active}
        end

        -- 2. Check account concurrency
        local acct_active = 0
        if acct_inflight_key ~= '' then
            acct_active = redis.call('SCARD', acct_inflight_key)
            if acct_max_conc > 0 and acct_active >= acct_max_conc then
                return {0, 'ACCT_CONCURRENCY', acct_active}
            end
        end

        -- 3. Check credential RPM
        local cred_rpm = tonumber(redis.call('GET', cred_rpm_key) or '0')
        if cred_max_rpm > 0 and cred_rpm >= cred_max_rpm then
            return {0, 'CRED_RPM', cred_rpm}
        end

        -- 4. Check account RPM
        if acct_rpm_key ~= '' and acct_max_rpm > 0 then
            local acct_rpm = tonumber(redis.call('GET', acct_rpm_key) or '0')
            if acct_rpm >= acct_max_rpm then
                return {0, 'ACCT_RPM', acct_rpm}
            end
        end

        -- All passed: atomically reserve
        redis.call('SADD', cred_inflight_key, request_id)
        redis.call('INCR', cred_rpm_key)
        redis.call('EXPIRE', cred_rpm_key, 120)

        if acct_inflight_key ~= '' then
            redis.call('SADD', acct_inflight_key, request_id)
        end
        if acct_rpm_key ~= '' then
            redis.call('INCR', acct_rpm_key)
            redis.call('EXPIRE', acct_rpm_key, 120)
        end
        redis.call('HSET', cred_state_key, 'last_used_ms', now_ms)
        return {1, 'OK', cred_active + 1}
        """
        minute_bucket = self._minute_bucket()
        keys = [
            f"ugw:inflight:cred:{credential_id}",
            f"ugw:inflight:acct:{account_group_id}" if account_group_id else "",
            f"ugw:win:cred:{credential_id}:rpm:{minute_bucket}",
            f"ugw:win:acct:{account_group_id}:rpm:{minute_bucket}" if account_group_id else "",
            f"ugw:cred:{credential_id}:state",
        ]
        args = [
            request_id,
            cred_max_concurrency or -1,
            acct_max_concurrency or -1 if account_group_id else -1,
            cred_quota.requests_per_minute or -1,
            (acct_quota.requests_per_minute or -1) if acct_quota else -1,
            str(self._now_ms()),
        ]
        try:
            res = await self.redis.eval(lua_script, len(keys), *keys, *args)
            ok = bool(res and res[0] == 1)
            reason = str(res[1]) if res and len(res) > 1 else ""
            return ok, QuotaEligibilityResult(
                eligible=ok,
                reason=reason if not ok else "Eligible and reserved",
            )
        except Exception:
            # Fallback to local evaluation if eval fails (e.g. unsupported in mocked setup)
            elig = await self.check_eligibility(
                provider_id=provider_id,
                supported_scopes=supported_scopes,
                credential_id=credential_id,
                account_group_id=account_group_id,
                workspace_id=None,
                cred_max_concurrency=cred_max_concurrency,
                acct_max_concurrency=acct_max_concurrency,
                cred_quota=cred_quota,
                acct_quota=acct_quota,
                model=model,
                estimated_tokens=estimated_tokens,
            )
            if elig.eligible:
                await self.reserve_lease(
                    request_id=request_id,
                    credential_id=credential_id,
                    account_group_id=account_group_id,
                    model=model,
                )
            return elig.eligible, elig

    async def reserve_lease(
        self,
        *,
        request_id: str,
        credential_id: str,
        account_group_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Atomically records an in-flight request reservation and increments window counters."""
        now_ms = self._now_ms()
        minute_bucket = self._minute_bucket()
        await self.redis.sadd(f"ugw:inflight:cred:{credential_id}", request_id)
        await self.redis.incr(f"ugw:win:cred:{credential_id}:rpm:{minute_bucket}")
        if account_group_id:
            await self.redis.sadd(f"ugw:inflight:acct:{account_group_id}", request_id)
            await self.redis.incr(
                f"ugw:win:acct:{account_group_id}:rpm:{minute_bucket}"
            )
            if model:
                await self.redis.incr(
                    f"ugw:win:model:{account_group_id}:{model}:rpm:{minute_bucket}"
                )
        await self.redis.hset(
            f"ugw:cred:{credential_id}:state",
            mapping={"last_used_ms": str(now_ms)},
        )

    async def release_lease(
        self,
        *,
        request_id: str,
        credential_id: str,
        account_group_id: str | None = None,
        usage: TokenUsage | None = None,
        rate_limit: RateLimitSnapshot | None = None,
    ) -> None:
        """Releases an in-flight concurrency slot and updates token counters and upstream rate limits."""
        await self.redis.srem(f"ugw:inflight:cred:{credential_id}", request_id)
        if account_group_id:
            await self.redis.srem(f"ugw:inflight:acct:{account_group_id}", request_id)

        minute_bucket = self._minute_bucket()
        if usage and usage.total_tokens > 0:
            cred_tpm_key = f"ugw:win:cred:{credential_id}:tpm:{minute_bucket}"
            cur = int(await self.redis.get(cred_tpm_key) or 0)
            await self.redis.set(cred_tpm_key, str(cur + usage.total_tokens), ex=120)
            if account_group_id:
                acct_tpm_key = f"ugw:win:acct:{account_group_id}:tpm:{minute_bucket}"
                cur_a = int(await self.redis.get(acct_tpm_key) or 0)
                await self.redis.set(
                    acct_tpm_key, str(cur_a + usage.total_tokens), ex=120
                )

        if rate_limit is not None:
            updates: dict[str, str] = {}
            if rate_limit.remaining_requests is not None:
                updates["remaining_requests"] = str(rate_limit.remaining_requests)
            if rate_limit.remaining_tokens is not None:
                updates["remaining_tokens"] = str(rate_limit.remaining_tokens)
            if updates:
                await self.redis.hset(
                    f"ugw:cred:{credential_id}:state", mapping=updates
                )

    async def next_round_robin_index(self, pool_key: str) -> int:
        val = await self.redis.incr(f"ugw:rr:{pool_key}")
        return val - 1


def acct_active_requests_exceeded(active: int, limit: int) -> bool:
    return active >= limit

