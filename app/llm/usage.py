"""Сохранение AIUsageEvent (§59 ТЗ).

Правило: каждый вызов провайдера возвращает и сохраняет usage record. Поэтому
запись делает сам адаптер (`app/llm/provider.py`), а не вызывающий код — иначе
любой новый workflow легко забыл бы про учёт.

Сбой записи расхода не должен ронять ответ студенту: исключение логируется, но
наружу не пробрасывается.
"""

import logging
from typing import Any

from sqlalchemy import func, select

from app.llm.registry import ModelProfile, cost_usd, to_rub
from app.observability.context import current
from config import settings
from db.models import AIUsageEvent
from db.session import async_session

logger = logging.getLogger(__name__)


async def record_usage(
    *,
    task: str,
    profile: ModelProfile,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    retry_count: int = 0,
    fallback_from: str | None = None,
    image_units: int = 0,
    audio_seconds: float = 0.0,
    error: str | None = None,
) -> None:
    ctx = current()
    usd = cost_usd(profile, input_tokens, cached_input_tokens, output_tokens)

    event = AIUsageEvent(
        user_id=ctx.user_id if ctx else None,
        request_id=ctx.request_id if ctx else "no-request-context",
        workflow=(ctx.workflow if ctx and ctx.workflow else task),
        task=task,
        provider=profile.provider,
        model=profile.model_id,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        image_units=image_units,
        audio_seconds=audio_seconds,
        provider_cost_usd=usd,
        provider_cost_rub=to_rub(usd),
        pricing_version=profile.pricing_version,
        latency_ms=latency_ms,
        retry_count=retry_count,
        fallback_from=fallback_from,
        prompt_version=settings.PROMPT_VERSION,
        retrieval_version=settings.RETRIEVAL_VERSION,
        channel=ctx.channel if ctx else "api",
        error=error,
    )

    try:
        async with async_session() as session:
            session.add(event)
            await session.commit()
    except Exception:
        # Телеметрия не должна стоить пользователю ответа.
        logger.exception("Не удалось сохранить AIUsageEvent (task=%s)", task)


async def usage_for_request(request_id: str) -> dict[str, Any]:
    """Фактические токены и стоимость одного запроса (по request_id) — для
    Playground/Answer Inspector (§8 дополнения к ТЗ) и evals/run.py (§39)."""
    async with async_session() as session:
        row = (
            await session.execute(
                select(
                    func.count(AIUsageEvent.id),
                    func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
                ).where(AIUsageEvent.request_id == request_id)
            )
        ).one()
    return {
        "calls": int(row[0]),
        "inputTokens": int(row[1]),
        "outputTokens": int(row[2]),
        "costRub": float(row[3]),
    }
