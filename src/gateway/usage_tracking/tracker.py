"""Usage Tracker and Cost Calculation Engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, desc, func, select

from gateway.core.canonical import TokenUsage
from gateway.core.db import Base, DatabaseManager
from gateway.core.exceptions import ResourceNotFoundError
from gateway.core.logging import get_logger
from gateway.model_registry.registry import ModelRegistry
from gateway.usage_tracking.models import (
    AnalyticsOverviewSummary,
    CredentialUsageBreakdown,
    ModelUsageBreakdown,
    ProviderUsageBreakdown,
    UsageEventInput,
    UsageRecordORM,
    UsageRecordView,
)

logger = get_logger("gateway.usage_tracker")


class UsageTracker:
    """Calculates request costs from verified model pricing and persists usage records."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        model_registry: ModelRegistry,
        *,
        flush_interval_seconds: float = 0.25,
        flush_batch_size: int = 100,
        queue_max_size: int = 10000,
    ) -> None:
        self._db_manager = db_manager
        self._model_registry = model_registry
        self._flush_interval_seconds = flush_interval_seconds
        self._flush_batch_size = flush_batch_size
        self._queue: asyncio.Queue[UsageRecordORM] = asyncio.Queue(maxsize=queue_max_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False

    def start_background_writer(self) -> None:
        """Starts the background worker task to flush usage records in batches."""
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._flush_worker())

    async def stop_background_writer(self) -> None:
        """Drains the queue and terminates the background worker cleanly."""
        self._stopping = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        # Drain any residual queued records on shutdown
        await self.flush()

    async def flush(self) -> None:
        """Drains all currently queued records and commits them in batches."""
        batch: list[UsageRecordORM] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._write_batch(batch)

    async def _flush_worker(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._flush_interval_seconds)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("usage_flush_worker_error", error=str(exc))

    async def _write_batch(self, batch: list[UsageRecordORM]) -> None:
        try:
            async with self._db_manager.session() as session:
                session.add_all(batch)
        except Exception as exc:
            logger.error("usage_batch_insert_failed", count=len(batch), error=str(exc))
        finally:
            for _ in batch:
                self._queue.task_done()

    async def ensure_tables(self) -> None:
        """Creates the gateway_usage_records table if not present."""
        engine = self._db_manager.engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def calculate_cost_usd(
        self,
        provider_id: str,
        model_id: str,
        usage: TokenUsage,
    ) -> float:
        """Calculates estimated USD cost from ModelRegistry pricing metadata.

        Never fabricates prices: if a model's cost metadata is None/UNKNOWN, returns 0.0.
        """
        canonical_id = (
            model_id if "/" in model_id else f"{provider_id}/{model_id}"
        )
        try:
            meta = self._model_registry.get_model(canonical_id)
        except ResourceNotFoundError:
            return 0.0

        cost_cfg = meta.cost
        total_usd = 0.0

        if cost_cfg.input_cost_per_1m_usd is not None and usage.input_tokens > 0:
            uncached_in = max(usage.input_tokens - usage.cached_input_tokens, 0)
            total_usd += (uncached_in / 1_000_000.0) * cost_cfg.input_cost_per_1m_usd
            if usage.cached_input_tokens > 0:
                cached_rate = (
                    cost_cfg.cached_input_cost_per_1m_usd
                    if cost_cfg.cached_input_cost_per_1m_usd is not None
                    else cost_cfg.input_cost_per_1m_usd
                )
                total_usd += (usage.cached_input_tokens / 1_000_000.0) * cached_rate

        if cost_cfg.output_cost_per_1m_usd is not None and usage.output_tokens > 0:
            total_usd += (usage.output_tokens / 1_000_000.0) * cost_cfg.output_cost_per_1m_usd

        if cost_cfg.per_character_usd is not None and usage.characters > 0:
            total_usd += usage.characters * cost_cfg.per_character_usd

        return round(total_usd, 8)

    def _build_orm_row(self, event: UsageEventInput) -> UsageRecordORM:
        total_tok = event.usage.total_tokens or (
            event.usage.input_tokens + event.usage.output_tokens
        )
        cost_usd = self.calculate_cost_usd(
            event.provider_id, event.model_id, event.usage
        )
        return UsageRecordORM(
            request_id=event.request_id,
            decision_id=event.decision_id,
            provider_id=event.provider_id,
            model_id=event.model_id,
            credential_id=event.credential_id,
            account_group_id=event.account_group_id,
            protocol=event.protocol,
            integration_mode=event.integration_mode,
            stream=event.stream,
            status_code=event.status_code,
            success=event.success,
            error_code=event.error_code,
            attempt_count=event.attempt_count,
            fallback_used=event.fallback_used,
            tool_rounds=event.tool_rounds,
            input_tokens=event.usage.input_tokens,
            output_tokens=event.usage.output_tokens,
            total_tokens=total_tok,
            cached_input_tokens=event.usage.cached_input_tokens,
            reasoning_tokens=event.usage.reasoning_tokens,
            characters=event.usage.characters,
            estimated_cost_usd=cost_usd,
            latency_ms=round(event.latency_ms, 2),
            ttft_ms=round(event.ttft_ms, 2) if event.ttft_ms is not None else None,
            client_key_id=event.client_key_id,
        )

    def enqueue_usage(self, event: UsageEventInput) -> float:
        """Non-blocking path: calculates estimated cost and buffers the record into the async queue.

        Returns the estimated cost in USD so metrics and span attributes can record it without waiting
        for database write completion. Falls back to synchronous DB insert if the queue is full.
        """
        orm_row = self._build_orm_row(event)
        try:
            self._queue.put_nowait(orm_row)
        except asyncio.QueueFull:
            # Fall back to immediate task if queue saturated
            asyncio.create_task(self._write_batch([orm_row]))
        return orm_row.estimated_cost_usd

    async def record_usage(self, event: UsageEventInput) -> UsageRecordView:
        """Computes cost and persists a UsageRecordORM entry immediately (used where exact row view is needed)."""
        orm_row = self._build_orm_row(event)
        async with self._db_manager.session() as session:
            session.add(orm_row)
            await session.flush()
            await session.refresh(orm_row)
            return self._to_view(orm_row)

    async def list_recent_requests(
        self,
        *,
        limit: int = 50,
        provider_id: str | None = None,
        success_only: bool | None = None,
    ) -> list[UsageRecordView]:
        await self.flush()
        async with self._db_manager.session() as session:
            stmt = select(UsageRecordORM).order_by(desc(UsageRecordORM.created_at)).limit(limit)
            if provider_id:
                stmt = stmt.where(UsageRecordORM.provider_id == provider_id)
            if success_only is not None:
                stmt = stmt.where(UsageRecordORM.success == success_only)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_view(r) for r in rows]

    async def get_overview_summary(self) -> AnalyticsOverviewSummary:
        await self.flush()
        async with self._db_manager.session() as session:
            stmt = select(
                func.count().label("total"),
                func.sum(case((UsageRecordORM.success.is_(True), 1), else_=0)).label("succ"),
                func.sum(case((UsageRecordORM.fallback_used.is_(True), 1), else_=0)).label("fb"),
                func.sum(UsageRecordORM.input_tokens).label("in_tok"),
                func.sum(UsageRecordORM.output_tokens).label("out_tok"),
                func.sum(UsageRecordORM.total_tokens).label("tot_tok"),
                func.sum(UsageRecordORM.characters).label("chars"),
                func.sum(UsageRecordORM.estimated_cost_usd).label("cost"),
                func.avg(UsageRecordORM.latency_ms).label("avg_lat"),
                func.avg(UsageRecordORM.ttft_ms).label("avg_ttft"),
            )
            row = (await session.execute(stmt)).first()
            if not row or not row.total:
                return AnalyticsOverviewSummary()

            total = int(row.total)
            succ = int(row.succ or 0)
            failed = total - succ
            fb = int(row.fb or 0)
            in_tok = int(row.in_tok or 0)
            out_tok = int(row.out_tok or 0)
            tot_tok = int(row.tot_tok or 0)
            chars = int(row.chars or 0)
            cost = round(float(row.cost or 0.0), 6)
            avg_lat = round(float(row.avg_lat or 0.0), 2)
            avg_ttft = round(float(row.avg_ttft), 2) if row.avg_ttft is not None else None

            return AnalyticsOverviewSummary(
                total_requests=total,
                successful_requests=succ,
                failed_requests=failed,
                success_rate=round(succ / total, 4) if total > 0 else 1.0,
                fallback_requests=fb,
                fallback_rate=round(fb / total, 4) if total > 0 else 0.0,
                total_input_tokens=in_tok,
                total_output_tokens=out_tok,
                total_tokens=tot_tok,
                total_characters=chars,
                total_estimated_cost_usd=cost,
                avg_latency_ms=avg_lat,
                avg_ttft_ms=avg_ttft,
            )

    async def get_provider_breakdown(self) -> list[ProviderUsageBreakdown]:
        await self.flush()
        async with self._db_manager.session() as session:
            stmt = (
                select(
                    UsageRecordORM.provider_id,
                    func.count().label("cnt"),
                    func.sum(case((UsageRecordORM.success.is_(True), 1), else_=0)).label("succ"),
                    func.sum(case((UsageRecordORM.fallback_used.is_(True), 1), else_=0)).label("fb"),
                    func.sum(UsageRecordORM.total_tokens).label("tot_tok"),
                    func.sum(UsageRecordORM.characters).label("chars"),
                    func.sum(UsageRecordORM.estimated_cost_usd).label("cost"),
                    func.avg(UsageRecordORM.latency_ms).label("avg_lat"),
                )
                .group_by(UsageRecordORM.provider_id)
                .order_by(UsageRecordORM.provider_id)
            )
            rows = (await session.execute(stmt)).all()
            result: list[ProviderUsageBreakdown] = []
            for r in rows:
                cnt = int(r.cnt)
                succ = int(r.succ or 0)
                result.append(
                    ProviderUsageBreakdown(
                        provider_id=r.provider_id,
                        request_count=cnt,
                        success_count=succ,
                        error_count=cnt - succ,
                        fallback_count=int(r.fb or 0),
                        total_tokens=int(r.tot_tok or 0),
                        total_characters=int(r.chars or 0),
                        estimated_cost_usd=round(float(r.cost or 0.0), 6),
                        avg_latency_ms=round(float(r.avg_lat or 0.0), 2),
                    )
                )
            return result

    async def get_model_breakdown(self) -> list[ModelUsageBreakdown]:
        await self.flush()
        async with self._db_manager.session() as session:
            stmt = (
                select(
                    UsageRecordORM.provider_id,
                    UsageRecordORM.model_id,
                    func.count().label("cnt"),
                    func.sum(case((UsageRecordORM.success.is_(True), 1), else_=0)).label("succ"),
                    func.sum(UsageRecordORM.input_tokens).label("in_tok"),
                    func.sum(UsageRecordORM.output_tokens).label("out_tok"),
                    func.sum(UsageRecordORM.total_tokens).label("tot_tok"),
                    func.sum(UsageRecordORM.estimated_cost_usd).label("cost"),
                    func.avg(UsageRecordORM.latency_ms).label("avg_lat"),
                    func.avg(UsageRecordORM.ttft_ms).label("avg_ttft"),
                )
                .group_by(UsageRecordORM.provider_id, UsageRecordORM.model_id)
                .order_by(UsageRecordORM.provider_id, UsageRecordORM.model_id)
            )
            rows = (await session.execute(stmt)).all()
            result: list[ModelUsageBreakdown] = []
            for r in rows:
                cnt = int(r.cnt)
                succ = int(r.succ or 0)
                result.append(
                    ModelUsageBreakdown(
                        model_id=r.model_id,
                        provider_id=r.provider_id,
                        request_count=cnt,
                        success_count=succ,
                        error_count=cnt - succ,
                        total_input_tokens=int(r.in_tok or 0),
                        total_output_tokens=int(r.out_tok or 0),
                        total_tokens=int(r.tot_tok or 0),
                        estimated_cost_usd=round(float(r.cost or 0.0), 6),
                        avg_latency_ms=round(float(r.avg_lat or 0.0), 2),
                        avg_ttft_ms=round(float(r.avg_ttft), 2) if r.avg_ttft is not None else None,
                    )
                )
            return result

    async def get_credential_breakdown(self) -> list[CredentialUsageBreakdown]:
        await self.flush()
        async with self._db_manager.session() as session:
            cred_col = func.coalesce(UsageRecordORM.credential_id, "unmanaged_or_passthrough").label("cid")
            stmt = (
                select(
                    UsageRecordORM.provider_id,
                    cred_col,
                    func.count().label("cnt"),
                    func.sum(case((UsageRecordORM.success.is_(True), 1), else_=0)).label("succ"),
                    func.sum(UsageRecordORM.total_tokens).label("tot_tok"),
                    func.sum(UsageRecordORM.estimated_cost_usd).label("cost"),
                )
                .group_by(UsageRecordORM.provider_id, cred_col)
                .order_by(UsageRecordORM.provider_id, cred_col)
            )
            rows = (await session.execute(stmt)).all()
            result: list[CredentialUsageBreakdown] = []
            for r in rows:
                cnt = int(r.cnt)
                succ = int(r.succ or 0)
                result.append(
                    CredentialUsageBreakdown(
                        credential_id=r.cid,
                        provider_id=r.provider_id,
                        request_count=cnt,
                        success_count=succ,
                        error_count=cnt - succ,
                        total_tokens=int(r.tot_tok or 0),
                        estimated_cost_usd=round(float(r.cost or 0.0), 6),
                    )
                )
            return result

    async def get_client_key_monthly_spend(self, client_key_id: str) -> float:
        """Returns the total estimated USD spent by a specific client key in the current calendar month."""
        await self.flush()
        now = datetime.now(timezone.utc)
        first_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        async with self._db_manager.session() as session:
            stmt = select(func.sum(UsageRecordORM.estimated_cost_usd)).where(
                UsageRecordORM.client_key_id == client_key_id,
                UsageRecordORM.created_at >= first_of_month,
            )
            spend = (await session.execute(stmt)).scalar()
            return float(spend or 0.0)

    def _to_view(self, row: UsageRecordORM) -> UsageRecordView:
        return UsageRecordView(
            id=row.id,
            request_id=row.request_id,
            decision_id=row.decision_id,
            provider_id=row.provider_id,
            model_id=row.model_id,
            credential_id=row.credential_id,
            account_group_id=row.account_group_id,
            protocol=row.protocol,
            integration_mode=row.integration_mode,
            stream=row.stream,
            status_code=row.status_code,
            success=row.success,
            error_code=row.error_code,
            attempt_count=row.attempt_count,
            fallback_used=row.fallback_used,
            tool_rounds=row.tool_rounds,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            cached_input_tokens=row.cached_input_tokens,
            reasoning_tokens=row.reasoning_tokens,
            characters=row.characters,
            estimated_cost_usd=row.estimated_cost_usd,
            latency_ms=row.latency_ms,
            ttft_ms=row.ttft_ms,
            created_at=row.created_at,
            client_key_id=row.client_key_id,
        )

