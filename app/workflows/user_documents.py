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

Батч 13: `ask_user_documents()` (во множественном числе) отвечает по НЕСКОЛЬКИМ
документам сразу — один `retrieve()`/один `generate_answer()` на объединённый
набор чанков, а не отдельный вызов на каждый документ. `ask_user_document()`
(один документ) остаётся без изменений для обратной совместимости.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.observability.context import current_request_id
from app.security.audit import audit
from app.workflows.ask import ask
from app.workflows.scope import Scope
from config import settings
from constants import (
    ALLOWED_UPLOAD_EXTENSIONS,
    SCOPE_DOCUMENTS,
    SOURCE_TEXTBOOK,
    SOURCE_USER_DOCUMENT,
)
from db.crud import delete_book
from db.models import Book, BookChunk
from db.session import async_session
from scripts.load_books import load_book

logger = logging.getLogger(__name__)

_NO_MATERIAL_MESSAGE = (
    "В твоём документе по этому вопросу ничего нет — попробуй переформулировать "
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


async def _owned_document_ids(user_id: str, document_ids: list[int]) -> list[int]:
    """Из запрошенных id оставляет только те, что реально принадлежат user_id.

    Одним батч-запросом, а не циклом по `_owns_document()` — id может быть
    много (батч 13, объединённый поиск). Чужой/несуществующий id молча
    выпадает из выдачи — тот же принцип «чужое = не найдено», что и у
    одиночного `ask_user_document()`, не ошибка на весь запрос.
    """
    if not document_ids:
        return []
    async with async_session() as session:
        stmt = (
            select(BookChunk.book_id)
            .where(BookChunk.book_id.in_(document_ids), BookChunk.user_id == user_id)
            .distinct()
        )
        rows = (await session.execute(stmt)).all()
    owned = {row.book_id for row in rows}
    # Порядок исходного списка сохраняется — пригодится, если вызывающая
    # сторона придаёт значение порядку документов (например, для отображения)
    return [doc_id for doc_id in document_ids if doc_id in owned]


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

    if len(await list_user_documents(user_id)) >= settings.USER_DOCUMENTS_MAX:
        return UserDocumentIngestResult(
            document=None,
            error=f"Достигнут предел — {settings.USER_DOCUMENTS_MAX} документов. "
            "Удали ненужный в панели «📄 Мои документы» и загрузи этот снова.",
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


async def resolve_scope(
    user_id: str,
    mode: str,
    document_ids: list[int],
    source_type: str | None = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> Scope:
    """Режим ответа студента → `Scope`. Из включённых документов остаются только реально
    принадлежащие user_id (удалённые/чужие id молча отбрасываются)."""
    owned = await _owned_document_ids(user_id, document_ids)
    return Scope(
        mode=mode,
        source_type=source_type,
        subject=subject,
        document_ids=tuple(owned),
        owner_id=user_id,
    )


async def ask_user_document(
    question: str,
    user_id: str,
    document_id: int,
    exam_id: str | None = None,
    turns: list[tuple[str, str]] | None = None,
) -> UserDocumentAskResult:
    return await ask_user_documents(question, user_id, [document_id], exam_id=exam_id, turns=turns)


async def ask_user_documents(
    question: str,
    user_id: str,
    document_ids: list[int],
    exam_id: str | None = None,
    turns: list[tuple[str, str]] | None = None,
) -> UserDocumentAskResult:
    """Вопрос по одному или НЕСКОЛЬКИМ документам студента (батч 13, 27).

    Идёт через общий конвейер `ask()` (память диалога, переписывание запроса, «да»-продолжение,
    несколько вопросов порциями, AnswerLog), а не отдельным коротким путём: один поиск по
    объединённому набору чанков всех выбранных документов и одна генерация. Изоляция:
    фильтры book_id + user_id остаются вместе в каждом SQL-запросе.
    """
    owned_ids = await _owned_document_ids(user_id, document_ids)
    if not owned_ids:
        return UserDocumentAskResult(answer="", error=_NOT_FOUND_MESSAGE, request_id=current_request_id())

    scope = Scope(
        mode=SCOPE_DOCUMENTS,
        source_type=SOURCE_USER_DOCUMENT,
        document_ids=tuple(owned_ids),
        owner_id=user_id,
    )
    result = await ask(question, source_type=SOURCE_USER_DOCUMENT, scope=scope, turns=turns)
    if result.answer is None:
        return UserDocumentAskResult(answer=_NO_MATERIAL_MESSAGE, request_id=result.request_id)

    return UserDocumentAskResult(
        answer=result.answer,
        verified=result.verified,
        evidence_references=result.citations if result.verified is not False else [],
        request_id=result.request_id,
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
