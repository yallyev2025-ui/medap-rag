"""Массовая загрузка клинических рекомендаций (PDF) в pgvector.

Раскладка папок (имя подпапки = категория, попадает в subject):

    клинреки/
    ├── взрослые/         → subject='взрослые'
    ├── дети/             → subject='дети'
    └── взрослые_и_дети/  → subject='взрослые_и_дети'

Запуск:
    python -m scripts.load_clinreks клинреки
    python -m scripts.load_clinreks /path/to/клинреки --workers 8 --batch-size 64

Скрипт возобновляемый: уже загруженные файлы (по book_title = имя файла)
пропускаются, поэтому его безопасно прерывать и запускать повторно.

Скорость: извлечение текста из PDF идёт параллельно по нескольким процессам
(--workers), а расчёт эмбеддингов — большими батчами на одной модели (подхватит
GPU автоматически, если он есть). Пишет в тот же DATABASE_URL, что и бот, поэтому
запускать можно как локально (на машине с GPU — в разы быстрее), так и на Railway.
"""

import argparse
import asyncio
import logging
import os
import traceback
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

from sqlalchemy import select

from constants import SOURCE_CLINREK
from db.models import Book, BookChunk
from db.session import async_session
from rag.embedder import embed_passages, _get_model
from rag.processor import chunk_text, extract_document

logger = logging.getLogger(__name__)

# Подпапки-категории. Имя папки становится значением subject у клинрека.
CATEGORY_SUBDIRS = ["взрослые", "дети", "взрослые_и_дети"]

ERRORS_LOG = "errors.log"
DEFAULT_BATCH_SIZE = 64
DEFAULT_WORKERS = min(8, os.cpu_count() or 1)


def _extract_worker(path_str: str) -> tuple[list[str], bool]:
    """Извлечение текста в отдельном процессе (без загрузки ML-модели)."""
    return extract_document(path_str)


async def _load_existing_titles() -> set[str]:
    async with async_session() as session:
        result = await session.execute(
            select(Book.title).where(Book.source_type == SOURCE_CLINREK)
        )
    return {row[0] for row in result.all()}


def _log_error(title: str, exc: Exception) -> None:
    with open(ERRORS_LOG, "a", encoding="utf-8") as f:
        f.write(f"=== {title} ===\n")
        f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        f.write("\n")


async def _insert_clinrek(title: str, subject: str, chunks, batch_size: int) -> int:
    """Считает эмбеддинги батчами и сохраняет книгу+чанки в БД. Возвращает число чанков."""
    async with async_session() as session:
        book = Book(
            title=title,
            author="",  # у клинреков нет автора — источником служит название рекомендации
            subject=subject,
            source_type=SOURCE_CLINREK,
            chunks_count=len(chunks),
        )
        session.add(book)
        await session.flush()
        book_id = book.id

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = await asyncio.to_thread(
                embed_passages, [c.content for c in batch]
            )
            for offset, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                session.add(
                    BookChunk(
                        book_id=book_id,
                        subject=subject,
                        author="",
                        title=title,
                        source_type=SOURCE_CLINREK,
                        chunk_index=start + offset,
                        content=chunk.content,
                        embedding=embedding,
                        page_from=chunk.page_from,
                        page_to=chunk.page_to,
                    )
                )
            await session.flush()

        await session.commit()
    return len(chunks)


def _collect_files(root: Path) -> list[tuple[Path, str]]:
    """Список (путь_к_pdf, subject) по всем подпапкам-категориям."""
    files: list[tuple[Path, str]] = []
    for subdir in CATEGORY_SUBDIRS:
        folder = root / subdir
        if not folder.is_dir():
            logger.warning("Подпапка не найдена, пропускаю: %s", folder)
            continue
        for pdf in sorted(folder.rglob("*.pdf")):
            files.append((pdf, subdir))
    return files


async def load_all(root: Path, workers: int, batch_size: int) -> None:
    files = _collect_files(root)
    total = len(files)
    if total == 0:
        print(f"В {root} не найдено PDF в подпапках {CATEGORY_SUBDIRS}.")
        return

    existing = await _load_existing_titles()
    # Загружаем модель заранее (один раз в главном процессе) — дальше переиспользуется.
    await asyncio.to_thread(_get_model)

    stats = {"ok": 0, "skipped": 0, "errors": 0, "done": 0}
    # Ограничивает число файлов «в полёте», чтобы извлечённый текст не копился в памяти.
    gate = asyncio.Semaphore(workers * 2)
    # Эмбеддинги и запись в БД сериализуем: модель одна, батчи должны идти по очереди.
    embed_lock = asyncio.Lock()

    loop = asyncio.get_running_loop()
    ctx = get_context("spawn")  # spawn безопасен рядом с CUDA/torch (в отличие от fork)

    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:

        async def process(pdf: Path, subject: str) -> None:
            title = pdf.stem
            if title in existing:
                stats["skipped"] += 1
                stats["done"] += 1
                print(f"⏭  Пропущен (уже загружен): {pdf.name} ({stats['done']}/{total})")
                return

            async with gate:
                try:
                    pages, paged = await loop.run_in_executor(
                        pool, _extract_worker, str(pdf)
                    )
                    chunks = await asyncio.to_thread(chunk_text, pages, paged=paged)
                    if not chunks:
                        raise ValueError("Не удалось извлечь текст из файла")
                    async with embed_lock:
                        n = await _insert_clinrek(title, subject, chunks, batch_size)
                    stats["ok"] += 1
                    stats["done"] += 1
                    print(f"✅ Обработан: {pdf.name} — {n} чанков ({stats['done']}/{total})")
                except Exception as exc:  # noqa: BLE001 — по ТЗ: логируем и продолжаем
                    stats["errors"] += 1
                    stats["done"] += 1
                    _log_error(title, exc)
                    print(f"❌ Ошибка: {pdf.name} ({stats['done']}/{total}) — см. {ERRORS_LOG}")

        await asyncio.gather(*(process(pdf, subj) for pdf, subj in files))

    print(
        "\nИтог: "
        f"успешно {stats['ok']}, пропущено {stats['skipped']}, ошибок {stats['errors']} "
        f"из {total}."
    )
    if stats["errors"]:
        print(f"Подробности по ошибкам: {ERRORS_LOG}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Массовая загрузка клинических рекомендаций (PDF) в pgvector"
    )
    parser.add_argument("folder", nargs="?", default="клинреки",
                        help="Путь к папке с подпапками взрослые/дети/взрослые_и_дети")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Процессов для извлечения PDF (по умолчанию {DEFAULT_WORKERS})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Размер батча эмбеддингов (по умолчанию {DEFAULT_BATCH_SIZE})")
    args = parser.parse_args()

    root = Path(args.folder)
    if not root.is_dir():
        parser.error(f"Папка не найдена: {root}")

    asyncio.run(load_all(root, args.workers, args.batch_size))


if __name__ == "__main__":
    main()
