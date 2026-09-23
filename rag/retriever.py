"""Гибридный поиск (§7 ТЗ): лексический BM25 + плотный вектор → RRF-фьюжн →
cross-encoder реранк.

BM25 использует встроенный полнотекстовый индекс Postgres (колонка content_tsv,
см. db/init_db.py) — отдельный поисковый движок (Elasticsearch и т.п.) не нужен.
Fusion — Reciprocal Rank Fusion (RRF), стандартный способ объединить два разных
ранжирования без калибровки шкал (у косинусной дистанции и ts_rank разные
единицы, складывать сырые скоры напрямую нельзя).
"""

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select

from config import settings
from constants import ACTIVE_SOURCE_STATUSES
from db.models import Book, BookChunk
from db.session import async_session
from rag.embedder import embed_query
from rag.reranker import rerank_scores

logger = logging.getLogger(__name__)

# Константа RRF-формулы 1/(k+rank): гасит влияние точного ранга у элементов
# далеко в хвосте списка, значение 60 — стандартный выбор из литературы по RRF.
RRF_K = 60


@dataclass
class ChunkResult:
    id: int
    content: str
    subject: str
    author: str
    title: str
    distance: float | None = None
    page_from: int | None = None
    page_to: int | None = None
    section: str | None = None
    rerank_score: float | None = None
    bm25_score: float | None = None
    # Есть ли фрагмент в дальнейшем в результатах BM25/dense — для диагностики
    # Retrieval Inspector (что именно нашло этот чанк).
    found_by: set[str] = field(default_factory=set)
    # book_id и уровень достоверности/проверки — нужны для честных citations
    # (§13 ТЗ, этап 3): evidenceId=id чанка, sourceId=book_id, оба — реальные id
    # из базы, а не порядковый номер, который нельзя проверить.
    book_id: int | None = None
    authority_level: str | None = None
    verification_status: str | None = None


def _base_filters(stmt, source_type: str | None, subject: str | None, title: str | None):
    # Отключённые/архивные источники не участвуют в поиске (§8 ТЗ, раздел 3
    # дополнения): фильтр по Book.status через JOIN — денормализовать статус в
    # каждый чанк не нужно, включение/отключение источника меняет одну строку.
    stmt = stmt.join(Book, Book.id == BookChunk.book_id).where(Book.status.in_(ACTIVE_SOURCE_STATUSES))
    if source_type is not None:
        stmt = stmt.where(BookChunk.source_type == source_type)
    if subject is not None:
        stmt = stmt.where(BookChunk.subject == subject)
    if title is not None:
        stmt = stmt.where(BookChunk.title == title)
    return stmt


async def _fetch_dense(
    question: str,
    limit: int,
    source_type: str | None,
    subject: str | None,
    title: str | None,
) -> list[ChunkResult]:
    query_embedding = embed_query(question)
    distance = BookChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = select(
        BookChunk.id,
        BookChunk.book_id,
        BookChunk.content,
        BookChunk.subject,
        BookChunk.author,
        BookChunk.title,
        BookChunk.page_from,
        BookChunk.page_to,
        BookChunk.section,
        BookChunk.authority_level,
        BookChunk.verification_status,
        distance,
    ).order_by(distance).limit(limit)
    stmt = _base_filters(stmt, source_type, subject, title)

    async with async_session() as session:
        rows = (await session.execute(stmt)).all()

    return [
        ChunkResult(
            id=row.id,
            book_id=row.book_id,
            content=row.content,
            subject=row.subject,
            author=row.author,
            title=row.title,
            distance=row.distance,
            page_from=row.page_from,
            page_to=row.page_to,
            section=row.section,
            authority_level=row.authority_level,
            verification_status=row.verification_status,
            found_by={"dense"},
        )
        for row in rows
    ]


async def _fetch_bm25(
    question: str,
    limit: int,
    source_type: str | None,
    subject: str | None,
    title: str | None,
) -> list[ChunkResult]:
    # websearch_to_tsquery терпимо к обычному пользовательскому вводу (кавычки,
    # дефисы, пунктуация) в отличие от строгого to_tsquery.
    tsquery = func.websearch_to_tsquery("russian", question)
    rank = func.ts_rank(BookChunk.content_tsv, tsquery).label("bm25_score")
    stmt = (
        select(
            BookChunk.id,
            BookChunk.book_id,
            BookChunk.content,
            BookChunk.subject,
            BookChunk.author,
            BookChunk.title,
            BookChunk.page_from,
            BookChunk.page_to,
            BookChunk.section,
            BookChunk.authority_level,
            BookChunk.verification_status,
            rank,
        )
        .where(BookChunk.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    stmt = _base_filters(stmt, source_type, subject, title)

    async with async_session() as session:
        try:
            rows = (await session.execute(stmt)).all()
        except Exception:
            # Пустой/чисто-стоп-словный запрос даёт пустой tsquery — не повод
            # проваливать весь retrieval, просто BM25-кандидатов не будет.
            logger.exception("BM25-поиск не выполнен, продолжаю только с вектором")
            return []

    return [
        ChunkResult(
            id=row.id,
            book_id=row.book_id,
            content=row.content,
            subject=row.subject,
            author=row.author,
            title=row.title,
            page_from=row.page_from,
            page_to=row.page_to,
            section=row.section,
            authority_level=row.authority_level,
            verification_status=row.verification_status,
            bm25_score=row.bm25_score,
            found_by={"bm25"},
        )
        for row in rows
    ]


def _rrf_fuse(dense: list[ChunkResult], bm25: list[ChunkResult]) -> list[ChunkResult]:
    """Reciprocal Rank Fusion: объединяет два ранжирования по позиции, а не по
    сырому скору (косинусная дистанция и ts_rank несравнимы напрямую)."""
    merged: dict[int, ChunkResult] = {}
    scores: dict[int, float] = {}

    for rank, chunk in enumerate(dense, start=1):
        merged[chunk.id] = chunk
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (RRF_K + rank)

    for rank, chunk in enumerate(bm25, start=1):
        if chunk.id in merged:
            merged[chunk.id].bm25_score = chunk.bm25_score
            merged[chunk.id].found_by |= chunk.found_by
        else:
            merged[chunk.id] = chunk
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (RRF_K + rank)

    ordered_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [merged[cid] for cid in ordered_ids]


async def _fetch_candidates(
    question: str,
    limit: int,
    source_type: str | None = None,
    subject: str | None = None,
    title: str | None = None,
) -> list[ChunkResult]:
    dense, bm25 = await asyncio.gather(
        _fetch_dense(question, limit, source_type, subject, title),
        _fetch_bm25(question, limit, source_type, subject, title),
    )
    if not dense and not bm25:
        return []
    return _rrf_fuse(dense, bm25)[:limit]


async def _rerank(question: str, chunks: list[ChunkResult]) -> list[ChunkResult]:
    """Переупорядочивает чанки cross-encoder реранкером (проставляет rerank_score).
    При недоступности реранкера мягко деградирует до порядка после RRF-фьюжна."""
    try:
        scores = await asyncio.to_thread(
            rerank_scores, question, [c.content for c in chunks]
        )
        for chunk, score in zip(chunks, scores):
            chunk.rerank_score = score
        chunks.sort(key=lambda c: c.rerank_score, reverse=True)
    except Exception:
        logger.exception("Реранкер недоступен, использую порядок после RRF-фьюжна")
    return chunks


def _is_relevant(chunk: ChunkResult) -> bool:
    if chunk.rerank_score is not None:
        return chunk.rerank_score >= settings.RERANK_SCORE_THRESHOLD
    if chunk.distance is not None:
        return chunk.distance <= settings.MAX_DISTANCE_THRESHOLD
    # Чанк найден только BM25 (нет векторной дистанции) и реранкер недоступен —
    # нет надёжного порога, считаем релевантным по самому факту лексического совпадения.
    return chunk.bm25_score is not None


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

    Кандидаты собираются гибридно (BM25 + вектор → RRF), поэтому находится и то,
    что раньше терялось из-за редкой терминологии, плохо ложащейся в эмбеддинг.
    Если реранкер недоступен — мягко деградируем до порядка после RRF-фьюжна,
    rerank_score остаётся None, а логика отказа падает обратно на пороги дистанции/BM25.
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


async def retrieve_with_diagnostics(
    question: str,
    candidates: int = settings.RETRIEVAL_CANDIDATES,
    top_k: int = settings.RERANK_TOP_K,
    source_type: str | None = None,
    subject: str | None = None,
) -> tuple[list[ChunkResult], dict]:
    """Как retrieve(), но без focus_document и с диагностикой по стадиям —
    для минимального Retrieval Inspector в админке (§7 ТЗ, раздел 5 дополнения).
    Не переиспользуется продовым workflow, чтобы не тратить лишний вектор/BM25
    запрос там, где diagnostics никто не читает.
    """
    dense, bm25 = await asyncio.gather(
        _fetch_dense(question, candidates, source_type, subject, None),
        _fetch_bm25(question, candidates, source_type, subject, None),
    )
    fused = _rrf_fuse(dense, bm25)[:candidates]
    final = await _rerank(question, list(fused))
    final = final[:top_k]

    diagnostics = {
        "dense_count": len(dense),
        "bm25_count": len(bm25),
        "fused_count": len(fused),
        "final": [
            {
                "id": c.id,
                "title": c.title,
                "page_from": c.page_from,
                "page_to": c.page_to,
                "section": c.section,
                "distance": c.distance,
                "bm25_score": c.bm25_score,
                "rerank_score": c.rerank_score,
                "found_by": sorted(c.found_by),
                "relevant": _is_relevant(c),
                "content": c.content,
            }
            for c in final
        ],
    }
    return final, diagnostics
