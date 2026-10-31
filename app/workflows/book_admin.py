"""Управление книгой с сайта (MedAP, этап 1): переобработка новой версией, описание рисунков,
переключение версии и отчёт по версиям. Владелец нажимает кнопку в Studio — консоль не нужна.

Тяжёлые задачи идут в фоне по одной (общий замок с загрузкой книг), состояние хранится в БД
(`BookTask`), поэтому пережидает перезапуск контейнера и виден на сайте.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from db.models import Book, BookChunk, BookFigure, BookTask
from db.session import async_session

logger = logging.getLogger(__name__)

KINDS = ("reprocess", "describe")
_background: set[asyncio.Task] = set()


class BookAdminError(ValueError):
    """Понятная владельцу причина отказа."""


@dataclass
class VersionReport:
    version: int
    active: bool
    chunks: int
    tables: int
    needs_review: int
    pages_covered: int
    figures: int


async def mark_stale_tasks_failed() -> int:
    """После перезапуска контейнера задачи в статусе «идёт» уже не выполняются — помечаем ошибкой."""
    async with async_session() as session:
        result = await session.execute(
            update(BookTask)
            .where(BookTask.status == "running")
            .values(status="error", detail="Прервано перезапуском сервера. Запустите ещё раз.", finished_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return result.rowcount or 0


async def version_reports(book_id: int) -> list[VersionReport]:
    async with async_session() as session:
        book = await session.get(Book, book_id)
        if book is None:
            raise BookAdminError("Книга не найдена")
        chunk_rows = await session.execute(
            select(
                BookChunk.processing_version,
                func.count().label("chunks"),
                func.count().filter(BookChunk.kind == "table").label("tables"),
                func.count().filter(BookChunk.needs_review.is_(True)).label("review"),
                func.count(func.distinct(BookChunk.page_from)).label("pages"),
            )
            .where(BookChunk.book_id == book_id)
            .group_by(BookChunk.processing_version)
            .order_by(BookChunk.processing_version)
        )
        figure_rows = await session.execute(
            select(BookFigure.processing_version, func.count()).where(BookFigure.book_id == book_id).group_by(BookFigure.processing_version)
        )
        figures = {version: count for version, count in figure_rows}
        return [
            VersionReport(
                version=r.processing_version,
                active=r.processing_version == book.active_version,
                chunks=r.chunks,
                tables=r.tables,
                needs_review=r.review,
                pages_covered=r.pages,
                figures=figures.get(r.processing_version, 0),
            )
            for r in chunk_rows
        ]


def switch_warning(reports: list[VersionReport], version: int) -> str | None:
    """Предупреждение перед переключением: новая версия не должна покрывать меньше страниц или давать
    заметно меньше фрагментов, чем действующая. `None` — замечаний нет."""
    active = next((r for r in reports if r.active), None)
    target = next((r for r in reports if r.version == version), None)
    if target is None:
        return f"У книги нет версии {version}."
    if active is None or active.version == version:
        return None
    problems = []
    if active.pages_covered and target.pages_covered < active.pages_covered * 0.95:
        problems.append(f"страниц охвачено {target.pages_covered} вместо {active.pages_covered}")
    if active.chunks and target.chunks < active.chunks * 0.8:
        problems.append(f"фрагментов {target.chunks} вместо {active.chunks}")
    return ("Новая версия хуже действующей: " + "; ".join(problems) + ".") if problems else None


async def switch_version(book_id: int, version: int, force: bool = False) -> dict:
    """Переключить поиск на версию книги. С замечанием отказывает, пока владелец не подтвердит (`force`)."""
    reports = await version_reports(book_id)
    warning = switch_warning(reports, version)
    if warning and not force:
        raise BookAdminError(warning + " Если всё равно нужно — подтвердите переключение.")
    from scripts.load_books import activate_version

    count = await activate_version(book_id, version)
    return {"version": version, "chunks": count}


async def list_tasks(book_id: int, limit: int = 10) -> list[BookTask]:
    async with async_session() as session:
        rows = await session.execute(select(BookTask).where(BookTask.book_id == book_id).order_by(BookTask.id.desc()).limit(limit))
        return list(rows.scalars().all())


async def start_task(book_id: int, kind: str) -> int:
    """Запустить фоновую задачу над книгой. Одна незавершённая задача на книгу."""
    if kind not in KINDS:
        raise BookAdminError("Неизвестная задача")
    async with async_session() as session:
        book = await session.get(Book, book_id)
        if book is None:
            raise BookAdminError("Книга не найдена")
        if kind == "reprocess" and not book.file_path:
            raise BookAdminError("Оригинал книги не сохранён в хранилище — переобработать нельзя")
        busy = await session.scalar(select(BookTask.id).where(BookTask.book_id == book_id, BookTask.status == "running").limit(1))
        if busy:
            raise BookAdminError("С этой книгой уже идёт задача — дождитесь её завершения")
        task = BookTask(book_id=book_id, kind=kind, status="running", detail="Запущено")
        session.add(task)
        await session.commit()
        task_id = task.id

    handle = asyncio.create_task(_run(task_id, book_id, kind))
    _background.add(handle)
    handle.add_done_callback(_background.discard)
    return task_id


async def _finish(task_id: int, status: str, detail: str) -> None:
    async with async_session() as session:
        await session.execute(
            update(BookTask).where(BookTask.id == task_id).values(status=status, detail=detail[:2000], finished_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def _run(task_id: int, book_id: int, kind: str) -> None:
    from scripts.load_books import SOURCE_INGEST_LOCK, reprocess_book

    try:
        async with SOURCE_INGEST_LOCK:  # общая очередь тяжёлых задач: бот и сайт остаются отзывчивыми
            if kind == "reprocess":
                result = await reprocess_book(book_id, activate=False)
                detail = (
                    f"Собрана версия {result['version']}: фрагментов {result['chunks']}, рисунков {result['figures']}. "
                    "Поиск пока отвечает по прежней версии — сверьте отчёт и переключите."
                )
            else:
                from app.workflows.figure_describe import describe_book_figures

                done = await describe_book_figures(book_id)
                detail = (
                    f"Описано моделью: {done['described']}, из кэша: {done['from_cache']}, "
                    f"пропущено: {done['skipped']}, сбоев: {done['failed']}."
                )
        await _finish(task_id, "done", detail)
    except Exception as exc:  # причина для владельца; подробности — в логе
        logger.exception("Задача над книгой %s (%s) упала", book_id, kind)
        await _finish(task_id, "error", str(exc) or exc.__class__.__name__)
