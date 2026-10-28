"""Массовая загрузка учебников папкой — для отдельной машины (не боевого сервера бота).

Раскладка:

    учебники/
    ├── анатомия/
    │   ├── Сапин - Анатомия человека.pdf
    │   └── Привес - Анатомия человека.pdf
    └── физиология/
        └── Агаджанян - Нормальная физиология.pdf

Имя подпапки — предмет (subject), имя файла «Автор - Название.ext» (если дефиса нет, автор «—», название — имя файла).
Форматы: PDF (в том числе сканы, они распознаются), .docx, .txt.

Запуск (на своём компьютере или арендованном сервере, где задан тот же DATABASE_URL, что у бота):
    python -m scripts.load_textbooks учебники
    python -m scripts.load_textbooks учебники --dry-run     # только показать, что будет загружено

Скрипт возобновляемый: книга с тем же названием и предметом пропускается, поэтому его можно прерывать и запускать снова.
На отдельной машине ограничения щадящего режима выключены (все ядра, без пауз): боевому боту эта машина не мешает.
Модель эмбеддингов должна быть ТА ЖЕ, что в боте (EMBEDDING_MODEL_NAME), иначе поиск перестанет находить загруженное.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import select

from config import settings
from constants import SOURCE_TEXTBOOK
from db.models import Book
from db.session import async_session
from scripts.load_books import load_book

logger = logging.getLogger(__name__)
EXTENSIONS = {".pdf", ".docx", ".txt"}


def parse_name(path: Path) -> tuple[str, str]:
    """«Автор - Название» → (автор, название)"""
    stem = path.stem.strip()
    if " - " in stem:
        author, title = stem.split(" - ", 1)
        return author.strip() or "—", title.strip() or stem
    return "—", stem


def collect(folder: Path) -> list[tuple[Path, str, str, str]]:
    items = []
    for subject_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        for f in sorted(subject_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in EXTENSIONS:
                author, title = parse_name(f)
                items.append((f, subject_dir.name, author, title))
    return items


async def already_loaded(subject: str, title: str) -> bool:
    async with async_session() as session:
        row = await session.execute(select(Book.id).where(Book.title == title, Book.subject == subject, Book.source_type == SOURCE_TEXTBOOK).limit(1))
        return row.first() is not None


async def run(folder: Path, dry_run: bool) -> int:
    items = collect(folder)
    if not items:
        print(f"В папке {folder} нет файлов (ожидается учебники/<предмет>/<файл>).")
        return 1
    print(f"Найдено файлов: {len(items)}")
    failed = 0
    for n, (path, subject, author, title) in enumerate(items, 1):
        label = f"[{n}/{len(items)}] {subject}: {author} — {title}"
        if await already_loaded(subject, title):
            print(f"{label}: уже загружено, пропускаю")
            continue
        if dry_run:
            print(f"{label}: будет загружено ({path.stat().st_size // 1024 // 1024} МБ)")
            continue
        print(f"{label}: загружаю…")
        try:
            _id, chunks = await load_book(str(path), subject, author, title, source_type=SOURCE_TEXTBOOK)
            print(f"{label}: готово, кусков {chunks}")
        except Exception as exc:  # одна плохая книга не должна останавливать остальные
            failed += 1
            logger.exception("Не загрузилась книга %s", path)
            print(f"{label}: ОШИБКА {exc}", file=sys.stderr)
    print(f"Готово. Ошибок: {failed}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Массовая загрузка учебников из папки")
    parser.add_argument("folder", help="Папка: учебники/<предмет>/<Автор - Название>.pdf")
    parser.add_argument("--dry-run", action="store_true", help="Только показать список")
    parser.add_argument("--gentle", action="store_true", help="Щадящий режим боевого сервера (4 ядра, паузы); по умолчанию выключен")
    args = parser.parse_args()
    if not args.gentle:
        settings.INGEST_THREADS = 0
        settings.INGEST_PAUSE_S = 0
        settings.OCR_WORKERS = max(settings.OCR_WORKERS, 4)
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(run(Path(args.folder), args.dry_run)))


if __name__ == "__main__":
    main()
