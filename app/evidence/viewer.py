"""Source Viewer lookup (§13 ТЗ, раздел 7 дополнения): по evidenceId — точная
страница/раздел/координаты фрагмента + presigned-ссылка на оригинал в S3.

Общий код для `GET /v1/evidence/{id}` (app/api/v1.py) и админки (Playground,
Answer Inspector показывают «Открыть оригинал» по каждой цитате) — один и тот же
DB-запрос и presign, не дублируем.
"""

from dataclasses import dataclass

from app.storage import s3
from db.models import Book, BookChunk
from db.session import async_session
from constants import SOURCE_USER_DOCUMENT


@dataclass
class EvidenceDetail:
    evidence_id: str
    source_id: str
    source_title: str
    author: str
    subject: str
    page: int | None
    page_to: int | None
    section: str | None
    exact_supporting_text: str
    char_start: int | None
    char_end: int | None
    authority_level: str
    verification_status: str
    # None — S3 не настроен (мягкая деградация, см. app/storage/s3.py) или у
    # источника ещё нет file_path.
    url: str | None = None
    # Владелец приватного фрагмента (личный документ студента); None у общих источников.
    # Клиенты, показывающие фрагмент конкретному человеку, обязаны сверить владельца.
    owner_id: str | None = None


async def fetch_evidence(
    evidence_id: int, *, owner_id: str | None = None, allow_private: bool = False,
) -> EvidenceDetail | None:
    async with async_session() as session:
        chunk = await session.get(BookChunk, evidence_id)
        if chunk is None:
            return None
        # Проверяем ДО чтения оригинала и создания подписанной ссылки.
        # Service-token удостоверяет сайт, а не владельца документа.
        private = chunk.user_id is not None or getattr(chunk, "source_type", None) == SOURCE_USER_DOCUMENT
        if private and not allow_private and (owner_id is None or chunk.user_id != owner_id):
            return None
        book = await session.get(Book, chunk.book_id)

    url = None
    if book is not None and book.file_path:
        url = s3.presigned_url(book.file_path)

    return EvidenceDetail(
        evidence_id=str(chunk.id),
        source_id=str(chunk.book_id),
        source_title=chunk.title,
        author=chunk.author,
        subject=chunk.subject,
        page=chunk.page_from,
        page_to=chunk.page_to,
        section=chunk.section,
        exact_supporting_text=chunk.content,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        authority_level=chunk.authority_level,
        verification_status=chunk.verification_status,
        url=url,
        owner_id=chunk.user_id,
    )
