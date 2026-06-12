"""CLI для загрузки PDF учебников в pgvector.

Использование:
    python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy \
        --author "Струков" --title "Патологическая анатомия"
"""

import argparse
import asyncio

from db.models import Book, BookChunk
from db.session import async_session
from rag.embedder import embed_passages
from rag.processor import chunk_text, extract_text

BATCH_SIZE = 32


async def load_book(pdf_path: str, subject: str, author: str, title: str) -> None:
    print(f"Извлекаю текст из {pdf_path}...")
    text = extract_text(pdf_path)

    print("Разбиваю на чанки...")
    chunks = chunk_text(text)
    print(f"Получено {len(chunks)} чанков.")

    async with async_session() as session:
        book = Book(title=title, author=author, subject=subject, chunks_count=len(chunks))
        session.add(book)
        await session.flush()
        book_id = book.id

        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            embeddings = embed_passages(batch)

            for offset, (chunk_content, embedding) in enumerate(zip(batch, embeddings)):
                session.add(
                    BookChunk(
                        book_id=book_id,
                        subject=subject,
                        author=author,
                        title=title,
                        chunk_index=batch_start + offset,
                        content=chunk_content,
                        embedding=embedding,
                    )
                )

            await session.commit()
            print(f"Обработано чанков: {min(batch_start + BATCH_SIZE, len(chunks))}/{len(chunks)}")

    print(f"Готово. Можно удалить исходный PDF: {pdf_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка PDF учебника в pgvector")
    parser.add_argument("--pdf", required=True, help="Путь к PDF-файлу")
    parser.add_argument("--subject", required=True, help="Предмет (pathanatomy, physiology, ...)")
    parser.add_argument("--author", required=True, help="Автор учебника")
    parser.add_argument("--title", required=True, help="Название учебника")
    args = parser.parse_args()

    asyncio.run(load_book(args.pdf, args.subject, args.author, args.title))


if __name__ == "__main__":
    main()
