"""CLI для загрузки PDF учебников в pgvector.

Использование:
    python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy \
        --author "Струков" --title "Патологическая анатомия"
"""

import argparse
import asyncio

from constants import SOURCE_TEXTBOOK
from db.models import Book, BookChunk
from db.session import async_session
from rag.embedder import embed_passages
from rag.processor import chunk_text, extract_document

BATCH_SIZE = 32


async def load_book(
    file_path: str,
    subject: str,
    author: str,
    title: str,
    source_type: str = SOURCE_TEXTBOOK,
) -> int:
    # Извлечение текста (включая OCR сканов) и чанкинг — тяжёлая синхронная работа,
    # уводим в поток, чтобы не блокировать event loop бота во время /addbook.
    pages, paged = await asyncio.to_thread(extract_document, file_path)
    chunks = await asyncio.to_thread(chunk_text, pages, paged=paged)

    if not chunks:
        raise ValueError("Не удалось извлечь текст из файла")

    async with async_session() as session:
        book = Book(
            title=title,
            author=author,
            subject=subject,
            source_type=source_type,
            chunks_count=len(chunks),
        )
        session.add(book)
        await session.flush()
        book_id = book.id

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
                        )
                    )

                await session.flush()
        except Exception:
            await session.rollback()
            raise

        await session.commit()

    return len(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка учебника (PDF/Word/txt) в pgvector")
    parser.add_argument("--file", "--pdf", dest="file", required=True,
                        help="Путь к файлу учебника (PDF, .docx или .txt)")
    parser.add_argument("--subject", required=True, help="Предмет (pathanatomy, physiology, ...)")
    parser.add_argument("--author", required=True, help="Автор учебника")
    parser.add_argument("--title", required=True, help="Название учебника")
    args = parser.parse_args()

    print(f"Извлекаю текст и разбиваю на чанки: {args.file}...")
    chunks_count = asyncio.run(load_book(args.file, args.subject, args.author, args.title))
    print(f"Готово. Добавлено чанков: {chunks_count}. Можно удалить исходный файл: {args.file}")


if __name__ == "__main__":
    main()
