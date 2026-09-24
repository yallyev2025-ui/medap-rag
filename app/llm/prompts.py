"""Версионирование системных промптов (раздел 10 дополнения к ТЗ), упрощённая
версия: редактирование текста в админке → публикация → откат к любой прошлой
версии, полная история. Без автоматического eval-гейта перед публикацией
(сознательное упрощение полного DRAFT→TEST→EVAL→PUBLISH — см.
plans/medap-ai/03_evidence.md).

Хардкод-константы в rag/generator.py остаются ДЕФОЛТОМ: если для ключа ещё нет
production-строки в БД, используется дефолт — ничего не ломается без единой
правки в админке.

Лёгкий in-process кэш (TTL): один процесс на всё приложение (как `_ingest_tasks`
в app/admin/routes.py), поэтому публикация из админки инвалидирует кэш сразу
через clear_cache(), а не ждёт TTL — он лишь подстраховка от лишних запросов к
БД на каждый вызов генерации.
"""

import time

from sqlalchemy import select

from db.models import PromptVersion
from db.session import async_session

_CACHE_TTL_SECONDS = 30
_cache: dict[str, tuple[str, float]] = {}


async def get_prompt(key: str, default: str) -> str:
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]

    async with async_session() as session:
        content = (
            await session.execute(
                select(PromptVersion.content)
                .where(PromptVersion.prompt_key == key, PromptVersion.status == "production")
                .order_by(PromptVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    resolved = content if content is not None else default
    _cache[key] = (resolved, now + _CACHE_TTL_SECONDS)
    return resolved


def clear_cache(key: str | None = None) -> None:
    """Вызывается сразу при публикации/откате из админки, чтобы новая версия
    подхватилась немедленно, а не через TTL."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)
