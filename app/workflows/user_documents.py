"""Документы пользователя (§18 ТЗ, этап 4A.5).

```
upload → malware/type/size validation → parse → structure extraction → chunk →
embed → private collection/namespace → retrieval
```

Изоляция: `user_id + document_id (=Book.id) + опциональный exam_id`. Документ одного
пользователя никогда не попадает в retrieval другого — `rag/retriever.py::retrieve()`
фильтрует по `book_id` И `user_id` одновременно в самом SQL-запросе (защита от
ошибки в проверке владения выше по стеку, а не только на уровне бизнес-логики этого
модуля). Удаление документа немедленно убирает его чанки и эмбеддинги (`db.crud.delete_book`
удаляет обе таблицы каскадно).

`BookChunk.user_id`/`exam_id` были зарезервированы под эту задачу ещё на этапе 2 —
здесь они наконец заполняются и используются как фильтр.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.llm.task_map import Task
from app.observability.context import current_request_id
from app.security.audit import audit
from config import settings
from constants import ALLOWED_UPLOAD_EXTENSIONS, SOURCE_USER_DOCUMENT
from db.crud import delete_book
from db.models import Book, BookChunk
from db.session import async_session
from rag.generator import generate_answer, relevant_chunks
from rag.retriever import retrieve
from scripts.load_books import load_book

logger = logging.getLogger(__name__)

_NO_MATERIAL_MESSAGE = (
    "В этом документе не нашлось материала по вопросу — попробуй переформулировать "
    "или уточнить, о какой части документа речь."
)
_NOT_FOUND_MESSAGE = "Документ не найден."


@dataclass
class UserDocument:
    id: int
    title: str
    subject: str | None
    chunks_count: int
    loaded_at: datetime | None


@dataclass
class UserDocumentIngestResult:
    document: UserDocument | None
    error: str | None = None
    request_id: str | None = None


@dataclass
class UserDocumentAskResult:
    answer: str
    verified: bool | None = None
    evidence_references: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    request_id: str | None = None


async def _owns_document(user_id: str, document_id: int) -> bool:
    async with async_session() as session:
        stmt = (
            select(BookChunk.id)
            .where(BookChunk.book_id == document_id, BookChunk.user_id == user_id)
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        return row is not None


async def ingest_user_document(
    file_path: str,
    filename: str,
    user_id: str,
    exam_id: str | None = None,
    title: str | None = None,
    subject: str | None = None,
) -> UserDocumentIngestResult:
    extension = os.path.splitext(filename)[1].lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        return UserDocumentIngestResult(
            document=None,
            error=f"Неподдерживаемый формат файла: {extension or 'без расширения'}. "
            f"Поддерживаются: {', '.join(ALLOWED_UPLOAD_EXTENSIONS)}.",
            request_id=current_request_id(),
        )

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > settings.USER_DOCUMENT_MAX_MB:
        return UserDocumentIngestResult(
            document=None,
            error=f"Файл больше {settings.USER_DOCUMENT_MAX_MB} МБ — загрузи документ поменьше.",
            request_id=current_request_id(),
        )

    resolved_title = title or os.path.splitext(filename)[0]
    try:
        book_id, chunks_count = await load_book(
            file_path,
            subject=subject or "личный_документ",
            author="студент",
            title=resolved_title,
            source_type=SOURCE_USER_DOCUMENT,
            authority_level="user_material",
            user_id=user_id,
            exam_id=exam_id,
        )
    except ValueError:
        logger.warning("Не удалось извлечь текст из документа пользователя: %s", resolved_title)
        return UserDocumentIngestResult(
            document=None,
            error="Не удалось прочитать документ — возможно, файл повреждён или пустой.",
            request_id=current_request_id(),
        )
    except Exception:
        logger.exception("Ошибка при загрузке документа пользователя")
        return UserDocumentIngestResult(
            document=None,
            error="Загрузка сейчас недоступна, попробуй позже.",
            request_id=current_request_id(),
        )

    await audit(
        "user_document_upload",
        actor=f"user:{user_id}",
        target=resolved_title,
        details=f"book_id={book_id} chunks={chunks_count}",
    )
    return UserDocumentIngestResult(
        document=UserDocument(
            id=book_id,
            title=resolved_title,
            subject=subject,
            chunks_count=chunks_count,
            loaded_at=datetime.now(timezone.utc),
        ),
        request_id=current_request_id(),
    )


async def ask_user_document(
    question: str,
    user_id: str,
    document_id: int,
    exam_id: str | None = None,
) -> UserDocumentAskResult:
    if not await _owns_document(user_id, document_id):
        return UserDocumentAskResult(answer="", error=_NOT_FOUND_MESSAGE, request_id=current_request_id())

    chunks = await retrieve(question, source_type=SOURCE_USER_DOCUMENT, book_id=document_id, user_id=user_id)
    relevant = relevant_chunks(chunks)
    if not relevant:
        return UserDocumentAskResult(answer=_NO_MATERIAL_MESSAGE, request_id=current_request_id())

    generated = await generate_answer(question, chunks, source_type=SOURCE_USER_DOCUMENT, task=Task.DOCUMENT_QA)

    citations: list[dict[str, Any]] = []
    if generated.verified is not False:
        cited_chunks = extract_cited_chunks(generated.text, relevant)
        citations = [c.to_dict() for c in build_citations(cited_chunks)]

    return UserDocumentAskResult(
        answer=generated.text,
        verified=generated.verified,
        evidence_references=citations,
        request_id=current_request_id(),
    )


async def delete_user_document(user_id: str, document_id: int) -> bool:
    if not await _owns_document(user_id, document_id):
        return False
    async with async_session() as session:
        title = await delete_book(session, document_id)
        await session.commit()
    await audit("user_document_delete", actor=f"user:{user_id}", target=title or str(document_id))
    return title is not None


async def list_user_documents(user_id: str) -> list[UserDocument]:
    async with async_session() as session:
        stmt = (
            select(Book.id, Book.title, Book.subject, Book.chunks_count, Book.loaded_at)
            .join(BookChunk, BookChunk.book_id == Book.id)
            .where(BookChunk.user_id == user_id, Book.source_type == SOURCE_USER_DOCUMENT)
            .distinct()
            .order_by(Book.loaded_at.desc())
        )
        rows = (await session.execute(stmt)).all()
    return [
        UserDocument(id=row.id, title=row.title, subject=row.subject, chunks_count=row.chunks_count, loaded_at=row.loaded_at)
        for row in rows
    ]
