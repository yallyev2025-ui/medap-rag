"""CLI для загрузки PDF учебников в pgvector.

Использование:
    python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy \
        --author "Струков" --title "Патологическая анатомия"
"""

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.storage import s3
from constants import DEFAULT_AUTHORITY_LEVEL, DEFAULT_VERIFICATION_STATUS, SOURCE_TEXTBOOK
from db.models import Book, BookChunk, BookFigure
from db.session import async_session
from config import settings
from rag.embedder import embed_passages, limited_threads
from rag.processor import chunk_text, extract_document

logger = logging.getLogger(__name__)

# Батч 17 поднимал это до 64 ради скорости; батч 18 откатывает обратно — на
# живом Timeweb (8 ГБ RAM) память во время загрузки одной книги и так ползла
# к пределу (см. expunge_all() в load_book()), рисковать пиковой памятью
# каждого отдельного вызова model.encode() ради небольшого выигрыша в
# скорости сейчас не стоит.
BATCH_SIZE = 32

# Батч 15: очередь на загрузку ИСТОЧНИКА (учебник/клинрек) в общий корпус —
# веб-админка (app/admin/routes.py::_run_ingest) и Telegram /addbook
# оборачивают им СВОЙ вызов load_book(), не саму функцию. Осознанно НЕ внутри
# load_book(): личные документы студента (ingest_user_document(),
# app/workflows/user_documents.py) тоже вызывают load_book(), и должны
# оставаться отзывчивыми для студента независимо от того, что в этот момент
# грузит админ — общий лок внутри самой функции придавил бы и их тоже.
SOURCE_INGEST_LOCK: asyncio.Lock = asyncio.Lock()


class DuplicateBookError(ValueError):
    """Такой файл уже загружен (по контрольной сумме). Второй раз грузить не нужно."""

    def __init__(self, book_id: int, title: str) -> None:
        super().__init__(f"Этот файл уже загружен как «{title}» (id {book_id}). Повторная загрузка не нужна.")
        self.book_id = book_id
        self.title = title


async def _find_duplicate(checksum: str) -> tuple[int, str] | None:
    async with async_session() as session:
        row = (
            await session.execute(
                select(Book.id, Book.title).where(Book.checksum == checksum, Book.status != "archived").limit(1)
            )
        ).first()
    return (row.id, row.title) if row else None


def _analyze_deep(file_path: str, pages: list[str]):
    """Глубокий разбор PDF (MedAP, этап 1): структура страниц, таблицы, вырезанные рисунки и
    страницы с плохим OCR. Любой сбой — None: загрузка идёт обычным путём, книга не теряется."""
    from rag import layout

    try:
        layouts = layout.analyze_pdf(file_path, with_figure_images=True, figure_dpi=settings.FIGURE_DPI)
    except Exception:
        logger.exception("Глубокий разбор не удался — книга грузится обычным путём")
        return None, set()
    scan_pages = [item.index for item in layouts if len(item.text.strip()) < 20]
    low = {page for page, conf in layout.ocr_page_confidences(file_path, scan_pages).items() if layout.needs_review(conf)}
    return layouts, low


async def _insert_chunks(session, *, book_id: int, book_meta: dict, chunks, version: int) -> None:
    """Эмбеддинги пачками и запись фрагментов книги (общий путь загрузки и переобработки)."""
    try:
        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            # щадящий режим: ограниченное число ядер и пауза между пачками, чтобы бот и сайт продолжали отвечать
            def _embed(texts: list[str] = [chunk.content for chunk in batch]) -> list[list[float]]:
                with limited_threads(settings.INGEST_THREADS):
                    return embed_passages(texts)

            embeddings = await asyncio.to_thread(_embed)
            await asyncio.sleep(settings.INGEST_PAUSE_S)

            for offset, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                session.add(
                    BookChunk(
                        book_id=book_id,
                        chunk_index=batch_start + offset,
                        content=chunk.content,
                        embedding=embedding,
                        page_from=chunk.page_from,
                        page_to=chunk.page_to,
                        section=chunk.section,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        processing_version=version,
                        kind=chunk.kind,
                        page_printed=chunk.page_printed,
                        section_path=chunk.section_path,
                        needs_review=chunk.needs_review,
                        **book_meta,
                    )
                )

            await session.flush()
            # Батч 18: flush() пишет в БД, но НЕ освобождает Python-объекты — уже добавленные
            # BookChunk (текст куска + вектор эмбеддинга) остались бы в identity map сессии до
            # самого commit() в конце. Для длинной книги (~1000+ кусков) это рост памяти монотонно
            # всю дорогу (подтверждено графиком памяти на живом Timeweb). expunge_all() снимает
            # сброшенные в БД объекты с учёта — пик памяти ограничен размером ОДНОЙ пачки.
            session.expunge_all()
    except Exception:
        await session.rollback()
        raise


def _figure_rows(book_id: int, version: int, layouts) -> list[tuple[BookFigure, bytes]]:
    """Рисунки глубокого разбора → строки каталога и картинки для S3. Повторяющиеся на многих
    страницах картинки (эмблемы, декор) отсеиваются; всего не больше MAX_FIGURES_PER_BOOK."""
    from PIL import Image

    seen: dict[str, int] = {}
    prepared = []
    for page in layouts:
        for number, figure in enumerate(page.figures, start=1):
            if not figure.png:
                continue
            sha = hashlib.sha256(figure.png).hexdigest()
            seen[sha] = seen.get(sha, 0) + 1
            if seen[sha] > 2:
                continue
            with Image.open(io.BytesIO(figure.png)) as image:
                width, height = image.size
            caption = figure.caption
            prepared.append(
                (
                    BookFigure(
                        book_id=book_id,
                        processing_version=version,
                        page=page.index,
                        page_printed=page.printed_page,
                        section_path=page.section_path,
                        kind="figure" if caption is None or caption.kind != "scheme" else "scheme",
                        number=caption.number if caption else None,
                        caption=caption.text if caption else None,
                        image_sha256=sha,
                        width=width,
                        height=height,
                        bbox=",".join(f"{v:.0f}" for v in figure.bbox),
                        s3_key=f"figures/{book_id}/v{version}/{page.index}-{number}.png",
                    ),
                    figure.png,
                )
            )
            if len(prepared) >= settings.MAX_FIGURES_PER_BOOK:
                return prepared
    return prepared


async def _save_figures(session, book_id: int, version: int, layouts) -> int:
    rows = _figure_rows(book_id, version, layouts)
    for figure, png in rows:
        key = await asyncio.to_thread(s3.upload_bytes, figure.s3_key, png)
        figure.s3_key = key  # None без S3: каталог остаётся, но ссылки на картинку не выдаются
        session.add(figure)
    await session.flush()
    return len(rows)


async def load_book(
    file_path: str,
    subject: str,
    author: str,
    title: str,
    source_type: str = SOURCE_TEXTBOOK,
    section: str | None = None,
    topic: str | None = None,
    edition: str | None = None,
    year: int | None = None,
    authority_level: str = DEFAULT_AUTHORITY_LEVEL,
    verification_status: str = DEFAULT_VERIFICATION_STATUS,
    language: str = "ru",
    user_id: str | None = None,
    exam_id: str | None = None,
    deep: bool | None = None,
    allow_duplicate: bool = False,
) -> tuple[int, int]:
    checksum = await asyncio.to_thread(s3.file_checksum, file_path)
    extension = os.path.splitext(file_path)[1].lower()

    # MedAP, этап 1: тот же файл второй раз не грузим (для общего корпуса; личные документы студента
    # и сознательная повторная загрузка — allow_duplicate — проходят как раньше)
    if user_id is None and not allow_duplicate:
        duplicate = await _find_duplicate(checksum)
        if duplicate:
            raise DuplicateBookError(*duplicate)

    # Извлечение текста (включая OCR сканов) и чанкинг — тяжёлая синхронная работа,
    # уводим в поток, чтобы не блокировать event loop бота во время /addbook.
    pages, paged = await asyncio.to_thread(extract_document, file_path)
    chunks = await asyncio.to_thread(chunk_text, pages, paged=paged)

    if not chunks:
        raise ValueError("Не удалось извлечь текст из файла")

    # Глубокий разбор — только PDF-учебники общего корпуса (не личные документы и не клинреки)
    use_deep = settings.DEEP_PARSE if deep is None else deep
    layouts = None
    if use_deep and extension == ".pdf" and user_id is None and source_type == SOURCE_TEXTBOOK:
        layouts, low_pages = await asyncio.to_thread(_analyze_deep, file_path, pages)
        if layouts is not None:
            from rag.layout import annotate_chunks

            chunks = annotate_chunks(chunks, layouts, low_pages)

    async with async_session() as session:
        book = Book(
            title=title,
            author=author,
            subject=subject,
            source_type=source_type,
            chunks_count=len(chunks),
            section=section,
            topic=topic,
            edition=edition,
            year=year,
            authority_level=authority_level,
            verification_status=verification_status,
            language=language,
            checksum=checksum,
            processing_version=1,
            active_version=1,
        )
        session.add(book)
        await session.flush()
        book_id = book.id

        # Оригинал и постраничный текст в S3 — best-effort, ключи из book_id
        # известны только после flush(). Без S3 остаются None (мягкая деградация,
        # см. app/storage/s3.py).
        book.file_path = await asyncio.to_thread(
            s3.upload_original, file_path, book_id, checksum, extension
        )
        book.pages_path = await asyncio.to_thread(
            s3.upload_text, json.dumps(pages, ensure_ascii=False), book_id, "pages.json"
        )

        await _insert_chunks(
            session,
            book_id=book_id,
            book_meta=dict(
                subject=subject, author=author, title=title, source_type=source_type, topic=topic, edition=edition,
                year=year, authority_level=authority_level, verification_status=verification_status,
                language=language, user_id=user_id, exam_id=exam_id,
            ),
            chunks=chunks,
            version=1,
        )
        if layouts is not None:
            await _save_figures(session, book_id, 1, layouts)

        await session.commit()

    return book_id, len(chunks)


async def reprocess_book(book_id: int, activate: bool = False) -> dict:
    """Переобработать книгу глубоким разбором как НОВУЮ версию рядом со старой (MedAP, этап 1).

    Старая версия продолжает отвечать на поиск, пока книгу не переключат (`activate_version`):
    сохранённые цитаты не ломаются, выдачу сначала сверяют по отчёту (docs/medap/plan, часть Б0).
    Оригинал берётся из S3 — без него переобработать нельзя.
    """
    async with async_session() as session:
        book = await session.get(Book, book_id)
        if book is None:
            raise ValueError("Книга не найдена")
        if not book.file_path:
            raise ValueError("Оригинал книги не сохранён в хранилище — переобработать нельзя")
        key = book.file_path
        version = book.processing_version + 1
        meta = dict(
            subject=book.subject, author=book.author, title=book.title, source_type=book.source_type,
            topic=book.topic, edition=book.edition, year=book.year, authority_level=book.authority_level,
            verification_status=book.verification_status, language=book.language, user_id=None, exam_id=None,
        )

    extension = os.path.splitext(key)[1].lower()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"book{extension}")
        if not await asyncio.to_thread(s3.download_to, key, path):
            raise ValueError("Не удалось скачать оригинал книги из хранилища")
        pages, paged = await asyncio.to_thread(extract_document, path)
        chunks = await asyncio.to_thread(chunk_text, pages, paged=paged)
        if not chunks:
            raise ValueError("Не удалось извлечь текст из файла")
        layouts = None
        if extension == ".pdf":
            layouts, low_pages = await asyncio.to_thread(_analyze_deep, path, pages)
            if layouts is not None:
                from rag.layout import annotate_chunks

                chunks = annotate_chunks(chunks, layouts, low_pages)

    async with async_session() as session:
        await _insert_chunks(session, book_id=book_id, book_meta=meta, chunks=chunks, version=version)
        figures = await _save_figures(session, book_id, version, layouts) if layouts is not None else 0
        book = await session.get(Book, book_id)
        book.processing_version = version
        await session.commit()

    if activate:
        await activate_version(book_id, version)
    return {"book_id": book_id, "version": version, "chunks": len(chunks), "figures": figures, "activated": activate}


async def activate_version(book_id: int, version: int) -> int:
    """Переключить книгу на версию разбора: с этого момента поиск отвечает по ней. Старые
    фрагменты остаются в базе (откат — повторное переключение на прежнюю версию)."""
    async with async_session() as session:
        count = await session.scalar(
            select(func.count()).select_from(BookChunk).where(BookChunk.book_id == book_id, BookChunk.processing_version == version)
        )
        if not count:
            raise ValueError(f"У книги нет фрагментов версии {version}")
        book = await session.get(Book, book_id)
        if book is None:
            raise ValueError("Книга не найдена")
        book.active_version = version
        book.chunks_count = count
        book.reindexed_at = datetime.now(timezone.utc)
        await session.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка учебника (PDF/Word/txt) в pgvector")
    parser.add_argument("--file", "--pdf", dest="file", required=True,
                        help="Путь к файлу учебника (PDF, .docx или .txt)")
    parser.add_argument("--subject", required=True, help="Предмет (pathanatomy, physiology, ...)")
    parser.add_argument("--author", required=True, help="Автор учебника")
    parser.add_argument("--title", required=True, help="Название учебника")
    parser.add_argument("--deep", action="store_true", help="Глубокий разбор: главы, страницы, таблицы, рисунки")
    parser.add_argument("--allow-duplicate", action="store_true", help="Загрузить, даже если такой файл уже есть")
    args = parser.parse_args()

    print(f"Извлекаю текст и разбиваю на чанки: {args.file}...")
    _book_id, chunks_count = asyncio.run(
        load_book(
            args.file, args.subject, args.author, args.title,
            deep=True if args.deep else None, allow_duplicate=args.allow_duplicate,
        )
    )
    print(f"Готово. Добавлено чанков: {chunks_count}. Можно удалить исходный файл: {args.file}")


if __name__ == "__main__":
    main()
