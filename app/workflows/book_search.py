"""Поиск по учебникам для сайта: предмет на сайте и предмет в базе учебников могут называться по-разному."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def search_with_subject_fallback(retrieve: Callable[..., Awaitable[list[Any]]], query: str, subject: str | None, source_type: str, top_k: int) -> list[Any]:
    """Сначала по предмету; если ничего нет — по всем учебникам, а не ответ «в учебниках нет» (владелец 02.11.2026)."""
    chunks = await retrieve(query, subject=subject, source_type=source_type, top_k=top_k)
    if not chunks and subject:
        logger.warning("Поиск по учебникам: по предмету %r ничего нет, повторяю без фильтра предмета", subject)
        chunks = await retrieve(query, subject=None, source_type=source_type, top_k=top_k)
    return chunks
