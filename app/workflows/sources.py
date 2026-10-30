"""Реестр источников и каталог рисунков для сайта (MedAP, этап 1).

Только чтение: сайт синхронизирует список книг, а при написании темы ищет рисунки учебника
по подписи и описанию. Права на показ определяет `Book.rights`: у книг `internal` ссылка
на картинку не выдаётся.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import func, or_, select

from app.storage import s3
from db.models import Book, BookChunk, BookFigure
from db.session import async_session

MAX_FIGURES = 50
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")


def parse_authors(authors_json: str | None, author: str) -> list[str]:
    """Список авторов: из JSON, иначе разбивка строки по запятым и точкам с запятой."""
    if authors_json:
        try:
            data = json.loads(authors_json)
            if isinstance(data, list) and all(isinstance(a, str) for a in data):
                return [a.strip() for a in data if a.strip()]
        except ValueError:
            pass
    return [a.strip() for a in re.split(r"[;,]", author or "") if a.strip()]


def query_terms(q: str | None) -> list[str]:
    """Слова запроса для поиска по подписи/описанию: нижний регистр, без коротких, не больше 8."""
    return [w.lower() for w in _WORD.findall(q or "")][:8]


@dataclass
class SourceItem:
    id: int
    title: str
    authors: list[str]
    subject: str
    source_type: str
    edition: str | None
    year: int | None
    publisher: str | None
    isbn: str | None
    authority_level: str
    rights: str
    status: str
    processing_version: int
    active_version: int
    chunks: int
    tables: int
    figures: int
    needs_review: int
    processed_at: str | None


async def list_sources(subject: str | None = None) -> list[SourceItem]:
    async with async_session() as session:
        stmt = select(Book).order_by(Book.subject, Book.id)
        if subject:
            stmt = stmt.where(Book.subject == subject)
        books = list((await session.execute(stmt)).scalars().all())
        ids = [b.id for b in books]
        if not ids:
            return []

        # Счётчики по активной версии книги: старые фрагменты в отчёт не попадают
        chunk_rows = await session.execute(
            select(
                BookChunk.book_id,
                func.count().label("chunks"),
                func.count().filter(BookChunk.kind == "table").label("tables"),
                func.count().filter(BookChunk.needs_review.is_(True)).label("review"),
            )
            .join(Book, Book.id == BookChunk.book_id)
            .where(BookChunk.book_id.in_(ids), BookChunk.processing_version == Book.active_version)
            .group_by(BookChunk.book_id)
        )
        chunk_counts = {r.book_id: r for r in chunk_rows}
        figure_rows = await session.execute(
            select(BookFigure.book_id, func.count())
            .join(Book, Book.id == BookFigure.book_id)
            .where(BookFigure.book_id.in_(ids), BookFigure.processing_version == Book.active_version)
            .group_by(BookFigure.book_id)
        )
        figure_counts = {book_id: n for book_id, n in figure_rows}

    items = []
    for b in books:
        c = chunk_counts.get(b.id)
        items.append(
            SourceItem(
                id=b.id,
                title=b.title,
                authors=parse_authors(b.authors_json, b.author),
                subject=b.subject,
                source_type=b.source_type,
                edition=b.edition,
                year=b.year,
                publisher=b.publisher,
                isbn=b.isbn,
                authority_level=b.authority_level,
                rights=b.rights,
                status=b.status,
                processing_version=b.processing_version,
                active_version=b.active_version,
                chunks=c.chunks if c else 0,
                tables=c.tables if c else 0,
                figures=figure_counts.get(b.id, 0),
                needs_review=c.review if c else 0,
                processed_at=b.reindexed_at.isoformat() if b.reindexed_at else None,
            )
        )
    return items


@dataclass
class FigureItem:
    id: int
    book_id: int
    book_title: str
    page: int
    page_printed: int | None
    section_path: str | None
    kind: str
    number: str | None
    caption: str | None
    description: str | None
    rights: str


async def search_figures(book_id: int | None, q: str | None, limit: int = 20, section: str | None = None) -> list[FigureItem]:
    """Рисунки активной версии: по книге, разделу (путь содержит строку) и словам запроса в
    подписи/описании/пути раздела. Без запроса — по порядку страниц."""
    limit = max(1, min(limit, MAX_FIGURES))
    terms = query_terms(q)
    async with async_session() as session:
        stmt = (
            select(BookFigure, Book.title, Book.rights)
            .join(Book, Book.id == BookFigure.book_id)
            .where(BookFigure.processing_version == Book.active_version, Book.status == "production")
            .order_by(BookFigure.book_id, BookFigure.page, BookFigure.id)
            .limit(limit)
        )
        if book_id is not None:
            stmt = stmt.where(BookFigure.book_id == book_id)
        if section:
            stmt = stmt.where(BookFigure.section_path.ilike(f"%{section}%"))
        if terms:
            stmt = stmt.where(
                or_(
                    *[
                        or_(
                            BookFigure.caption.ilike(f"%{t}%"),
                            BookFigure.description.ilike(f"%{t}%"),
                            BookFigure.section_path.ilike(f"%{t}%"),
                        )
                        for t in terms
                    ]
                )
            )
        rows = (await session.execute(stmt)).all()
    return [
        FigureItem(
            id=f.id,
            book_id=f.book_id,
            book_title=title,
            page=f.page,
            page_printed=f.page_printed,
            section_path=f.section_path,
            kind=f.kind,
            number=f.number,
            caption=f.caption,
            description=f.description,
            rights=rights,
        )
        for f, title, rights in rows
    ]


async def get_figure_url(figure_id: int) -> tuple[FigureItem, str | None] | None:
    """Рисунок и временная ссылка на картинку. Для книг с правами `internal` ссылка не выдаётся."""
    async with async_session() as session:
        figure = await session.get(BookFigure, figure_id)
        if figure is None:
            return None
        book = await session.get(Book, figure.book_id)
    if book is None:
        return None
    item = FigureItem(
        id=figure.id,
        book_id=figure.book_id,
        book_title=book.title,
        page=figure.page,
        page_printed=figure.page_printed,
        section_path=figure.section_path,
        kind=figure.kind,
        number=figure.number,
        caption=figure.caption,
        description=figure.description,
        rights=book.rights,
    )
    url = s3.presigned_url(figure.s3_key) if figure.s3_key and book.rights == "show" else None
    return item, url
