"""System Health (раздел 14 дополнения к ТЗ, этап 4B.5): живой снимок состояния
инфраструктуры — БД, провайдеры, S3, очередь загрузки. Не история расхода (для
этого Dashboard, app/admin/stats.py::dashboard_stats()) — это «работает ли всё
прямо сейчас», а не «сколько было потрачено за месяц».
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.llm.registry import model_registry
from app.storage import s3
from db.models import IngestJob
from db.session import async_session

logger = logging.getLogger(__name__)

JOB_STATUSES = ("pending", "running", "done", "error")


@dataclass
class ProviderHealth:
    provider: str
    model_id: str
    enabled: bool


@dataclass
class SystemHealth:
    db_ok: bool
    db_latency_ms: float | None = None
    db_error: str | None = None
    providers: list[ProviderHealth] = field(default_factory=list)
    s3_configured: bool = False
    jobs_by_status: dict[str, int] = field(default_factory=dict)
    stuck_jobs: int = 0


async def system_health() -> SystemHealth:
    health = SystemHealth(db_ok=True, jobs_by_status={status: 0 for status in JOB_STATUSES})

    started = time.monotonic()
    try:
        async with async_session() as session:
            rows = await session.execute(
                select(IngestJob.status, func.count(IngestJob.id)).group_by(IngestJob.status)
            )
            for status, count in rows:
                health.jobs_by_status[status] = count
        health.db_latency_ms = round((time.monotonic() - started) * 1000, 1)
    except Exception as exc:
        logger.exception("System Health: БД недоступна")
        health.db_ok = False
        health.db_error = str(exc)

    health.stuck_jobs = health.jobs_by_status.get("error", 0)
    health.providers = [
        ProviderHealth(provider=key, model_id=prof.model_id, enabled=prof.enabled)
        for key, prof in model_registry().items()
    ]
    health.s3_configured = s3.is_configured()
    return health
