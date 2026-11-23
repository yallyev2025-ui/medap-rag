"""Материал клинических рекомендаций в ответе Ментора (Mentor 2.0, этап 3).

Зачем отдельный модуль. Кнопка «КР РФ» уже переключает поиск на загруженные
клинические рекомендации, но загруженный PDF — не единственный источник: на
clinreky.ru лежит разобранная страница с разделами и оригинальными пунктами
рекомендаций. Этот модуль добавляет её материал к ответу, когда режим КР включён.

Правила:
- ничего не выдумываем: страница не открылась или разобралась с ошибкой — просто
  не добавляем материал (ответ пойдёт прежним путём);
- берём оригинальный текст рекомендаций, а не ИИ-выжимки (это уже делает
  `app.integrations.clinreky`);
- чужой документ не показываем: у публичных `owner_id=None`, приватные проверяет
  хранилище;
- синтетические фрагменты получают отрицательный id. По нему `fetch_evidence`
  отдаёт раздел разобранного документа, поэтому ссылка «Открыть источник» не
  ведёт в пустоту.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import select

from app.documents.clinreky_pure import (
    GuidelineCandidate,
    page_fragments,
    pick_candidate,
    select_fragments,
)
from app.integrations.clinreky import BASE, ClinrekyPage, Recommendation, fetch_catalog_items
from constants import ACTIVE_SOURCE_STATUSES, SOURCE_CLINREK
from db.models import Book
from db.session import async_session

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from rag.retriever import ChunkResult

logger = logging.getLogger(__name__)

# Сколько фрагментов разобранной страницы добавлять к ответу.
WITH_BOOK_CHUNKS = 4
WITHOUT_BOOK_CHUNKS = 24

# Сколько дней доверять сохранённому разбору, не перечитывая страницу.
# Публичные КР меняются редакциями, а не каждый день; после TTL — один запрос
# на проверку, и только при изменении текста появляется новая запись.
CLINREKY_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class ClinrekyMaterial:
    chunks: tuple[Any, ...]
    title: str
    url: str
    kr_id: str
    edition: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "krId": self.kr_id,
            "edition": self.edition,
            "chunks": len(self.chunks),
        }


def _as_chunks(fragments: list[dict[str, Any]]) -> list["ChunkResult"]:
    # Импорт внутри функции: `rag.retriever` тянет torch, а тесты чистых
    # помощников не должны его поднимать.
    from rag.retriever import ChunkResult

    return [
        ChunkResult(
            id=int(fragment["id"]),
            content=str(fragment["content"]),
            subject=str(fragment["subject"]),
            author=str(fragment["author"]),
            title=str(fragment["title"]),
            section=str(fragment["section"]),
            book_id=int(fragment["book_id"]),
            authority_level=str(fragment["authority_level"]),
            verification_status=str(fragment["verification_status"]),
        )
        for fragment in fragments
    ]


async def _candidate_from_relevant(relevant: Sequence[Any]) -> GuidelineCandidate | None:
    book_id = next((chunk.book_id for chunk in relevant if getattr(chunk, "book_id", None) is not None), None)
    if book_id is None:
        return None
    async with async_session() as session:
        book = await session.get(Book, book_id)
        if book is None or not book.external_ref:
            return None

        return GuidelineCandidate(book.id, book.title, book.external_ref, book.subject, book.author)


async def _candidate_from_title(question: str) -> GuidelineCandidate | None:
    """Запасной путь: ищем рекомендацию по названию среди активных клинреков."""
    async with async_session() as session:
        books = (
            await session.execute(
                select(Book).where(
                    Book.source_type == SOURCE_CLINREK,
                    Book.status.in_(ACTIVE_SOURCE_STATUSES),
                    Book.external_ref.is_not(None),
                )
            )
        ).scalars().all()

    candidates = [
        GuidelineCandidate(book.id, book.title, book.external_ref or "", book.subject, book.author)
        for book in books
        if book.external_ref
    ]

    return pick_candidate(question, candidates)


def _age_subject(age: str) -> str:
    """Категория пациента так, как её понимает `subject` у клинреков."""
    return {"adult": "взрослые", "child": "дети", "mixed": "взрослые_и_дети"}.get(age, "взрослые_и_дети")


async def _candidate_from_catalog(question: str) -> GuidelineCandidate | None:
    """Запасной путь без загруженных книг: каталог клинреков на самом сайте.

    Каталог — один JSON, а не обход 700 страниц. Выбор рекомендации идёт
    сравнением названий, без вызова модели: платить за «какую страницу
    открыть» не нужно.
    """
    items = await fetch_catalog_items()
    candidates = [
        GuidelineCandidate(
            book_id=0,
            title=item["title"],
            external_ref=str(item.get("id") or ""),
            subject=_age_subject(str(item.get("age") or "")),
            author="Минздрав России",
            url=str(item.get("url") or ""),
        )
        for item in items
        if item.get("title") and item.get("url")
    ]
    if not candidates:
        return None

    return pick_candidate(question, candidates)


async def material_for(
    question: str,
    *,
    relevant: Sequence[Any] = (),
    book_id: int | None = None,
) -> ClinrekyMaterial | None:
    """Разобрать рекомендацию по вопросу и вернуть её фрагменты (или None)."""
    candidate = await _candidate_from_relevant(relevant)
    if candidate is None and book_id is not None:
        async with async_session() as session:
            book = await session.get(Book, book_id)
        if book is not None and book.external_ref:
            candidate = GuidelineCandidate(book.id, book.title, book.external_ref, book.subject, book.author)
    if candidate is None:
        candidate = await _candidate_from_title(question)
    if candidate is None:
        candidate = await _candidate_from_catalog(question)
    if candidate is None:
        return None

    url = candidate.url or ""
    if not url:
        code = candidate.external_ref.rsplit("_", 1)[0].strip()
        if not code:
            return None
        url = f"{BASE}/nosology/{code}/"

    # Импорт внутри функции: тесты разбора страницы не должны тянуть БД.
    from app.documents.store import read_clinreky

    stored = await read_clinreky(url, max_age_seconds=CLINREKY_CACHE_TTL_SECONDS)
    if stored is None or stored.status == "failed":
        return None

    page = ClinrekyPage(
        url=stored.url,
        title=stored.title,
        kr_id=stored.kr_id,
        edition=stored.edition,
        sections=[dict(section) for section in stored.sections],
        recommendations=[
            Recommendation(
                text=str(item.get("text") or ""),
                level=item.get("level"),
                section=item.get("section"),
            )
            for item in stored.recommendations
            if isinstance(item, dict)
        ],
        tables=[dict(table) for table in stored.tables],
        official_url=stored.official_url,
        status=stored.status,
        missing=list(stored.missing),
    )

    limit = WITH_BOOK_CHUNKS if relevant else WITHOUT_BOOK_CHUNKS
    fragments = select_fragments(
        page_fragments(
            page,
            document_id=stored.id,
            book_id=candidate.book_id,
            subject=candidate.subject,
            author=candidate.author or "Минздрав России",
        ),
        question,
        limit,
        # Не тащим в промпт всю рекомендацию: 30 тыс. знаков хватает на
        # несколько разделов и десятки пунктов, а счёт за модель не растёт
        # вместе с объёмом документа.
        max_chars=30_000,
    )
    if not fragments:
        return None

    return ClinrekyMaterial(
        chunks=tuple(_as_chunks(fragments)),
        title=stored.title,
        url=stored.url,
        kr_id=stored.kr_id,
        edition=stored.edition,
    )


async def add_material(
    question: str,
    chunks: list[Any],
    relevant: list[Any],
) -> tuple[list[Any], ClinrekyMaterial | None]:
    """Добавить разобранную рекомендацию к выдаче; ошибка сети ответ не отменяет."""
    try:
        material = await material_for(question, relevant=relevant)
    except Exception:
        logger.warning("clinreky: не удалось добавить материал к ответу", exc_info=False)

        return chunks, None
    if material is None:
        return chunks, None

    return chunks + list(material.chunks), material
