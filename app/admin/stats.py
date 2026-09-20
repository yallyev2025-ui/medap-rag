"""Агрегаты расхода для Dashboard и Usage & Costs (§59 ТЗ, раздел 14 дополнения).

Считаются прямо в Postgres: перцентили через `percentile_cont`, а не выгрузкой
всех строк в память — таблица расхода растёт быстрее остальных.

Источник истины — `ai_usage_events` плюс версионированные цены из конфига; в
бизнес-логике фиксированных «0.14 ₽» не хранится (§65).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select

from db.models import AIUsageEvent
from db.session import async_session


@dataclass
class DashboardStats:
    requests_today: int = 0
    active_users_today: int = 0
    errors_today: int = 0
    cost_today_rub: float = 0.0
    cost_month_rub: float = 0.0
    cost_by_provider_rub: dict[str, float] = field(default_factory=dict)
    cost_per_active_user_rub: float = 0.0
    p50_rub: float = 0.0
    p90_rub: float = 0.0
    p99_rub: float = 0.0
    projected_month_rub: float = 0.0
    fallback_share: float = 0.0
    top_workflows: list[dict[str, Any]] = field(default_factory=list)
    top_users: list[dict[str, Any]] = field(default_factory=list)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def dashboard_stats() -> DashboardStats:
    now = datetime.now(timezone.utc)
    day_start = now - timedelta(days=1)
    month_start = _month_start(now)
    stats = DashboardStats()

    async with async_session() as session:
        today = (
            await session.execute(
                select(
                    func.count(AIUsageEvent.id),
                    func.count(func.distinct(AIUsageEvent.user_id)),
                    func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
                    func.coalesce(func.sum(case((AIUsageEvent.error.is_not(None), 1), else_=0)), 0),
                ).where(AIUsageEvent.created_at >= day_start)
            )
        ).one()
        stats.requests_today, stats.active_users_today, stats.cost_today_rub, stats.errors_today = (
            today[0],
            today[1],
            float(today[2]),
            int(today[3]),
        )

        month = (
            await session.execute(
                select(
                    func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
                    func.count(func.distinct(AIUsageEvent.user_id)),
                    func.coalesce(func.sum(case((AIUsageEvent.fallback_from.is_not(None), 1), else_=0)), 0),
                    func.count(AIUsageEvent.id),
                ).where(AIUsageEvent.created_at >= month_start)
            )
        ).one()
        stats.cost_month_rub = float(month[0])
        active_month = int(month[1]) or 0
        fallbacks, total_calls = int(month[2]), int(month[3])
        stats.fallback_share = (fallbacks / total_calls) if total_calls else 0.0
        stats.cost_per_active_user_rub = (stats.cost_month_rub / active_month) if active_month else 0.0

        by_provider = await session.execute(
            select(
                AIUsageEvent.provider,
                func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
            )
            .where(AIUsageEvent.created_at >= month_start)
            .group_by(AIUsageEvent.provider)
        )
        stats.cost_by_provider_rub = {row[0]: float(row[1]) for row in by_provider}

        # Перцентили стоимости одного вызова: средний запрос ничего не говорит о
        # тяжёлых пользователях, поэтому ТЗ требует смотреть P90/P99 (§58).
        percentiles = (
            await session.execute(
                select(
                    func.percentile_cont(0.5).within_group(AIUsageEvent.provider_cost_rub),
                    func.percentile_cont(0.9).within_group(AIUsageEvent.provider_cost_rub),
                    func.percentile_cont(0.99).within_group(AIUsageEvent.provider_cost_rub),
                ).where(AIUsageEvent.created_at >= month_start)
            )
        ).one()
        stats.p50_rub = float(percentiles[0] or 0.0)
        stats.p90_rub = float(percentiles[1] or 0.0)
        stats.p99_rub = float(percentiles[2] or 0.0)

        workflows = await session.execute(
            select(
                AIUsageEvent.workflow,
                func.count(AIUsageEvent.id),
                func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
            )
            .where(AIUsageEvent.created_at >= month_start)
            .group_by(AIUsageEvent.workflow)
            .order_by(func.sum(AIUsageEvent.provider_cost_rub).desc())
            .limit(10)
        )
        stats.top_workflows = [
            {"workflow": row[0], "calls": row[1], "costRub": float(row[2])} for row in workflows
        ]

        users = await session.execute(
            select(
                AIUsageEvent.user_id,
                func.count(AIUsageEvent.id),
                func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
            )
            .where(AIUsageEvent.created_at >= month_start, AIUsageEvent.user_id.is_not(None))
            .group_by(AIUsageEvent.user_id)
            .order_by(func.sum(AIUsageEvent.provider_cost_rub).desc())
            .limit(10)
        )
        stats.top_users = [
            {"userId": row[0], "calls": row[1], "costRub": float(row[2])} for row in users
        ]

    # Линейная экстраполяция расхода на месяц по уже прошедшим дням.
    days_passed = max((now - month_start).total_seconds() / 86400, 0.5)
    days_in_month = 30.4
    stats.projected_month_rub = stats.cost_month_rub / days_passed * days_in_month

    return stats
