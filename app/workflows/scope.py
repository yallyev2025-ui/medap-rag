"""Откуда отвечать (батч 27): учебники / мои документы / мои документы и учебники.

`Scope` — выбор студента из панели Telegram (или параметры API). Здесь же поиск по личным
документам: маленький документ отдаётся модели целиком, большой — ищется по нескольким
формулировкам с мягким порогом и соседними фрагментами. Фильтры book_id + user_id
остаются вместе в каждом запросе — изоляция документов (§18) не ослабляется.
"""

import logging
from dataclasses import dataclass

from config import settings
from constants import (
    SCOPE_BOTH,
    SCOPE_DOCUMENTS,
    SCOPE_TEXTBOOK,
    SOURCE_TEXTBOOK,
    SOURCE_USER_DOCUMENT,
)
from rag.generator import document_threshold, expand_queries, relevant_chunks
from rag.retriever import (
    ChunkResult,
    count_document_chunks,
    fetch_document_chunks,
    fetch_neighbors,
    retrieve_union,
)

logger = logging.getLogger(__name__)

# Сколько соседних фрагментов максимум добавляем к найденным (потолок контекста).
_MAX_NEIGHBORS = 10
_NEIGHBOR_SEEDS = 8


@dataclass(frozen=True)
class Scope:
    mode: str = SCOPE_TEXTBOOK
    # Учебники/клинреки: тип и предмет (в режиме «документы» не используются).
    source_type: str | None = SOURCE_TEXTBOOK
    subject: str | None = None
    # Включённые документы студента и их владелец (оба нужны вместе).
    document_ids: tuple[int, ...] = ()
    owner_id: str | None = None

    @property
    def uses_documents(self) -> bool:
        return self.mode in (SCOPE_DOCUMENTS, SCOPE_BOTH) and bool(self.document_ids) and bool(self.owner_id)

    @property
    def uses_textbooks(self) -> bool:
        return self.mode in (SCOPE_TEXTBOOK, SCOPE_BOTH)


async def retrieve_documents(
    question: str,
    document_ids: tuple[int, ...] | list[int],
    owner_id: str,
    turns: list[tuple[str, str]] | None = None,
) -> tuple[list[ChunkResult], dict]:
    """Фрагменты документов студента для ответа + диагностика.

    Небольшие документы (≤ DOC_FULL_CONTEXT_CHUNKS фрагментов суммарно) — целиком, без
    поиска: модель сама находит нужное, а «ответ был, но не нашёлся» исключён. Большие —
    несколько формулировок, один реранк, мягкий порог, соседние фрагменты.
    """
    ids = list(document_ids)
    total = await count_document_chunks(ids, owner_id)
    if total == 0:
        return [], {"mode": "empty", "total": 0}

    if total <= settings.DOC_FULL_CONTEXT_CHUNKS:
        chunks = await fetch_document_chunks(ids, owner_id)
        return chunks, {"mode": "full", "total": total, "chunks": len(chunks)}

    queries = await expand_queries(question, turns)
    found = await retrieve_union(
        queries,
        rerank_with=question,
        candidates=settings.RETRIEVAL_CANDIDATES,
        top_k=settings.RERANK_TOP_K,
        source_type=SOURCE_USER_DOCUMENT,
        book_id=ids,
        user_id=owner_id,
    )
    relevant = relevant_chunks(found, document_threshold())
    neighbors = await fetch_neighbors(relevant[:_NEIGHBOR_SEEDS], owner_id, score=document_threshold())
    selected = relevant + neighbors[:_MAX_NEIGHBORS]
    # В порядке документа: мысль конспекта читается связно, а не россыпью по рангу.
    selected.sort(key=lambda c: (c.book_id or 0, c.chunk_index if c.chunk_index is not None else 0))
    return selected, {
        "mode": "search",
        "total": total,
        "queries": len(queries),
        "found": len(found),
        "relevant": len(relevant),
        "neighbors": min(len(neighbors), _MAX_NEIGHBORS),
    }
