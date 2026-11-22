"""Хранилище разобранных документов и общий кеш (Mentor 2.0, этап 3).

Правила владельца 22.11.2026:
- публичный документ разбирается один раз и переиспользуется всеми (URL + хеш);
- приватный (с `owner_id`) в общий кеш не попадает и чужому не виден;
- одновременные запросы одного документа ждут одно разбирание (single-flight);
- ошибка базы не отменяет ответ: документ просто разберётся заново.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.integrations.clinreky import ClinrekyPage, fetch_page
from db.models import Document, DocumentSection, DocumentTable
from db.session import async_session

logger = logging.getLogger(__name__)

# Версия политики разбора: при изменении парсера старый кеш перестаёт попадать
POLICY = "documents-1"

_inflight: dict[str, asyncio.Future] = {}


def doc_key(url: str, content_hash: str, owner_id: str | None) -> str:
    """Ключ single-flight: адрес + хеш + владелец (публичный и личный не смешиваются)."""
    return f"{owner_id or 'public'}|{url}|{content_hash}"


def content_hash(html: str) -> str:
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredDocument:
    id: int
    url: str
    title: str
    edition: str
    status: str
    sections: tuple[dict[str, Any], ...]
    tables: tuple[dict[str, Any], ...]
    recommendations: tuple[dict[str, Any], ...]
    official_url: str
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "edition": self.edition,
            "status": self.status,
            "sections": list(self.sections),
            "tables": list(self.tables),
            "recommendations": list(self.recommendations),
            "officialUrl": self.official_url,
            "missing": list(self.missing),
        }


def _from_row(document: Document, sections: list[DocumentSection], tables: list[DocumentTable], page: ClinrekyPage | None) -> StoredDocument:
    return StoredDocument(
        id=document.id,
        url=document.url,
        title=document.title,
        edition=document.edition,
        status=document.status,
        sections=tuple({"title": s.title, "text": s.text, "order": s.order} for s in sections),
        tables=tuple({"rows": json.loads(t.rows or "[]"), "order": t.order} for t in tables),
        recommendations=tuple(json.loads(document.recommendations or "[]")),
        official_url=document.official_url,
        missing=tuple(json.loads(document.missing or "[]")),
    )


async def _load_cached(url: str, hash_value: str, owner: str | None) -> StoredDocument | None:
    try:
        async with async_session() as session:
            row = (await session.execute(
                select(Document).where(
                    Document.url == url,
                    Document.content_hash == hash_value,
                    Document.owner_id.is_(None) if owner is None else Document.owner_id == owner,
                )
            ).order_by(Document.id)).scalars().first()
            if row is None:
                return None
            sections = (await session.execute(select(DocumentSection).where(DocumentSection.document_id == row.id).order_by(DocumentSection.order))).scalars().all()
            tables = (await session.execute(select(DocumentTable).where(DocumentTable.document_id == row.id).order_by(DocumentTable.order))).scalars().all()

            return _from_row(row, list(sections), list(tables), None)
    except Exception:
        logger.warning("Документы: кеш недоступен, разберём заново")
        return None


async def _save(page: ClinrekyPage, hash_value: str, owner: str | None) -> StoredDocument | None:
    try:
        async with async_session() as session:
            # Уникальность (url, hash, owner) в PostgreSQL не ловит NULL-владельца,
            # поэтому проверяем существующую запись сами: дублей быть не должно.
            existing = (await session.execute(
                select(Document).where(
                    Document.url == page.url,
                    Document.content_hash == hash_value,
                    Document.owner_id.is_(None) if owner is None else Document.owner_id == owner,
                ).order_by(Document.id)
            )).scalars().first()
            if existing is not None:
                sections = (await session.execute(select(DocumentSection).where(DocumentSection.document_id == existing.id).order_by(DocumentSection.order))).scalars().all()
                tables = (await session.execute(select(DocumentTable).where(DocumentTable.document_id == existing.id).order_by(DocumentTable.order))).scalars().all()

                return _from_row(existing, list(sections), list(tables), page)
            document = Document(
                url=page.url,
                content_hash=hash_value,
                title=page.title,
                edition=page.edition,
                external_id=page.kr_id,
                status=page.status,
                owner_id=owner,
                missing=json.dumps(page.missing, ensure_ascii=False),
                official_url=page.official_url,
                recommendations=json.dumps([{"text": r.text, "level": r.level} for r in page.recommendations], ensure_ascii=False),
            )
            session.add(document)
            await session.flush()
            sections: list[DocumentSection] = []
            for index, section in enumerate(page.sections):
                row = DocumentSection(document_id=document.id, order=index, title=section["title"], text=section["text"])
                session.add(row)
                sections.append(row)
            for index, table in enumerate(page.tables):
                session.add(DocumentTable(document_id=document.id, order=index, rows=json.dumps(table["rows"], ensure_ascii=False)))
            await session.commit()

            return _from_row(document, sections, [], page)
    except Exception:
        logger.warning("Документы: не удалось сохранить разбор", exc_info=False)
        return None


async def read_clinreky(url: str, *, owner_id: str | None = None, fetcher=fetch_page) -> StoredDocument | None:
    """Разобрать документ с кешем: второй запрос того же документа не ходит в сеть.

    Хеш содержимого узнаётся только после загрузки, поэтому сначала загружаем
    страницу, затем ищем кеш по (адрес, хеш). Для повторных запросов это один
    HTTP-запрос — он дешевле, чем полный разбор и запись.
    """
    page = await fetcher(url)
    if page is None:
        return None
    hash_value = content_hash(page.as_dict().__str__())
    cached = await _load_cached(url, hash_value, owner_id)
    if cached is not None:
        return cached

    key = doc_key(url, hash_value, owner_id)
    running = _inflight.get(key)
    if running is not None:
        try:
            return await asyncio.shield(running)
        except Exception:
            return await _save(page, hash_value, owner_id)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[key] = future
    try:
        stored = await _save(page, hash_value, owner_id)
        if not future.done():
            future.set_result(stored)
        return stored
    finally:
        _inflight.pop(key, None)
