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
    title: str | None = None,
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

    if title is not None:
        stmt = stmt.where(BookChunk.title == title)

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


async def _rerank(question: str, chunks: list[ChunkResult]) -> list[ChunkResult]:
    """Переупорядочивает чанки cross-encoder реранкером (проставляет rerank_score).
    При недоступности реранкера мягко деградирует до порядка по векторной дистанции."""
    try:
        scores = await asyncio.to_thread(
            rerank_scores, question, [c.content for c in chunks]
        )
        for chunk, score in zip(chunks, scores):
            chunk.rerank_score = score
        chunks.sort(key=lambda c: c.rerank_score, reverse=True)
    except Exception:
        logger.exception("Реранкер недоступен, использую порядок по векторной дистанции")
    return chunks


def _is_relevant(chunk: ChunkResult) -> bool:
    if chunk.rerank_score is not None:
        return chunk.rerank_score >= settings.RERANK_SCORE_THRESHOLD
    return chunk.distance <= settings.MAX_DISTANCE_THRESHOLD


async def retrieve(
    question: str,
    candidates: int = settings.RETRIEVAL_CANDIDATES,
    top_k: int = settings.RERANK_TOP_K,
    source_type: str | None = None,
    subject: str | None = None,
    focus_document: bool = False,
) -> list[ChunkResult]:
    """Возвращает top_k фрагментов, переупорядоченных реранкером (rerank_score проставлен).

    source_type/subject задают логическую базу: например (учебник, physiology) или
    (клинрек, взрослые). Оба None — поиск по всему (обратная совместимость).

    focus_document=True (для клинреков): после реранка бот определяет ОДНУ самую
    релевантную рекомендацию и глубоко добирает материал строго из неё. Это убирает
    «мешанину» из разных рекомендаций и даёт врачу точный ответ из одного документа.

    Если реранкер недоступен (например, не хватило RAM на загрузку модели) — мягко
    деградируем до порядка по векторной дистанции, rerank_score остаётся None,
    а логика отказа падает обратно на косинусный порог.
    """
    chunk_list = await _fetch_candidates(question, candidates, source_type, subject)
    if not chunk_list:
        return []

    chunk_list = await _rerank(question, chunk_list)
    top = chunk_list[:top_k]

    if not focus_document:
        return top

    # Выбираем доминирующую рекомендацию по верхнему релевантному чанку и добираем
    # из неё больше контекста для глубокого ответа. Если релевантного нет — вернём
    # как есть (дальше сработает честный отказ/фолбэк).
    relevant = [c for c in top if _is_relevant(c)]
    if not relevant:
        return top

    dominant_title = relevant[0].title
    doc_chunks = await _fetch_candidates(
        question, candidates, source_type, subject, title=dominant_title
    )
    doc_chunks = await _rerank(question, doc_chunks)
    return doc_chunks[: settings.CLINREK_TOP_K]
