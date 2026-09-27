"""CLI для загрузки PDF учебников в pgvector.

Использование:
    python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy \
        --author "Струков" --title "Патологическая анатомия"
"""

import argparse
import asyncio
import json
import os

from app.storage import s3
from constants import DEFAULT_AUTHORITY_LEVEL, DEFAULT_VERIFICATION_STATUS, SOURCE_TEXTBOOK
from db.models import Book, BookChunk
from db.session import async_session
from rag.embedder import embed_passages
from rag.processor import chunk_text, extract_document

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
) -> tuple[int, int]:
    # Извлечение текста (включая OCR сканов) и чанкинг — тяжёлая синхронная работа,
    # уводим в поток, чтобы не блокировать event loop бота во время /addbook.
    pages, paged = await asyncio.to_thread(extract_document, file_path)
    chunks = await asyncio.to_thread(chunk_text, pages, paged=paged)

    if not chunks:
        raise ValueError("Не удалось извлечь текст из файла")

    checksum = await asyncio.to_thread(s3.file_checksum, file_path)
    extension = os.path.splitext(file_path)[1].lower()

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

        try:
            for batch_start in range(0, len(chunks), BATCH_SIZE):
                batch = chunks[batch_start : batch_start + BATCH_SIZE]
                embeddings = await asyncio.to_thread(
                    embed_passages, [chunk.content for chunk in batch]
                )

                for offset, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                    session.add(
                        BookChunk(
                            book_id=book_id,
                            subject=subject,
                            author=author,
                            title=title,
                            source_type=source_type,
                            chunk_index=batch_start + offset,
                            content=chunk.content,
                            embedding=embedding,
                            page_from=chunk.page_from,
                            page_to=chunk.page_to,
                            section=chunk.section,
                            topic=topic,
                            edition=edition,
                            year=year,
                            authority_level=authority_level,
                            verification_status=verification_status,
                            language=language,
                            char_start=chunk.char_start,
                            char_end=chunk.char_end,
                            user_id=user_id,
                            exam_id=exam_id,
                        )
                    )

                await session.flush()
                # Батч 18: flush() пишет в БД, но НЕ освобождает Python-объекты —
                # уже добавленные BookChunk (текст куска + вектор эмбеддинга)
                # остались бы в identity map сессии до самого commit() в конце
                # функции. Для длинной книги (~1000+ кусков) это рост памяти
                # монотонно всю дорогу, а не разовый скачок — подтверждено
                # графиком памяти на живом Timeweb (занятая память ползла к
                # пределу тарифа по ходу загрузки одной книги). expunge_all()
                # снимает уже сброшенные в БД объекты с учёта сессии — пик
                # памяти теперь ограничен размером ОДНОЙ пачки, а не всей
                # книгой целиком, независимо от числа страниц.
                session.expunge_all()
        except Exception:
            await session.rollback()
            raise

        await session.commit()

    return book_id, len(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка учебника (PDF/Word/txt) в pgvector")
    parser.add_argument("--file", "--pdf", dest="file", required=True,
                        help="Путь к файлу учебника (PDF, .docx или .txt)")
    parser.add_argument("--subject", required=True, help="Предмет (pathanatomy, physiology, ...)")
    parser.add_argument("--author", required=True, help="Автор учебника")
    parser.add_argument("--title", required=True, help="Название учебника")
    args = parser.parse_args()

    print(f"Извлекаю текст и разбиваю на чанки: {args.file}...")
    _book_id, chunks_count = asyncio.run(load_book(args.file, args.subject, args.author, args.title))
    print(f"Готово. Добавлено чанков: {chunks_count}. Можно удалить исходный файл: {args.file}")


if __name__ == "__main__":
    main()
