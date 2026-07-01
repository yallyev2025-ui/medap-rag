"""Двухэтапный поиск: pgvector-кандидаты → cross-encoder реранк."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from config import settings
from db.models import BookChunk
from db.session import async_session
from rag.embedder import embed_query
from rag.reranker import rerank_scores

logger = logging.getLogger(__name__)


@dataclass
class ChunkResult:
    content: str
    subject: str
    author: str
    title: str
    distance: float
    page_from: int | None = None
    page_to: int | None = None
    rerank_score: float | None = None


async def _fetch_candidates(
    question: str,
    limit: int,
    source_type: str | None = None,
    subject: str | None = None,
) -> list[ChunkResult]:
    query_embedding = embed_query(question)

    distance = BookChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(
            BookChunk.content,
            BookChunk.subject,
            BookChunk.author,
            BookChunk.title,
            BookChunk.page_from,
            BookChunk.page_to,
            distance,
        )
        .order_by(distance)
        .limit(limit)
    )
    # Жёсткое разделение баз: учебники и клинреки никогда не смешиваются, а внутри
    # режима поиск идёт строго по выбранному предмету/категории (subject=None —
    # без фильтра по предмету, например «Все категории» клинреков).
    if source_type is not None:
        stmt = stmt.where(BookChunk.source_type == source_type)
    if subject is not None:
        stmt = stmt.where(BookChunk.subject == subject)

    async with async_session() as session:
        result = await session.execute(stmt)
        rows = result.all()

    return [
        ChunkResult(
            content=row.content,
            subject=row.subject,
            author=row.author,
            title=row.title,
            distance=row.distance,
            page_from=row.page_from,
            page_to=row.page_to,
        )
        for row in rows
    ]


async def retrieve(
    question: str,
    candidates: int = settings.RETRIEVAL_CANDIDATES,
    top_k: int = settings.RERANK_TOP_K,
    source_type: str | None = None,
    subject: str | None = None,
) -> list[ChunkResult]:
    """Возвращает top_k фрагментов, переупорядоченных реранкером (rerank_score проставлен).

    source_type/subject задают логическую базу: например (учебник, physiology) или
    (клинрек, взрослые). Оба None — поиск по всему (обратная совместимость).

    Если реранкер недоступен (например, не хватило RAM на загрузку модели) — мягко
    деградируем до порядка по векторной дистанции, rerank_score остаётся None,
    а логика отказа падает обратно на косинусный порог.
    """
    chunk_list = await _fetch_candidates(question, candidates, source_type, subject)
    if not chunk_list:
        return []

    try:
        scores = await asyncio.to_thread(
            rerank_scores, question, [c.content for c in chunk_list]
        )
        for chunk, score in zip(chunk_list, scores):
            chunk.rerank_score = score
        chunk_list.sort(key=lambda c: c.rerank_score, reverse=True)
    except Exception:
        logger.exception("Реранкер недоступен, использую порядок по векторной дистанции")

    return chunk_list[:top_k]
